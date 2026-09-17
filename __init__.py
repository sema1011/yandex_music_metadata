from functools import partial
from urllib.parse import urlencode

from PyQt6.QtWidgets import (
    QCheckBox, QLabel, QLineEdit, QVBoxLayout,
)

from picard.plugin3.api import (
    CoverArtImage,
    CoverArtProvider,
    OptionsPage,
    t_,
)


# ═══════════════════════════════════════════════════════════════════════
#  Вспомогательные функции
# ═══════════════════════════════════════════════════════════════════════

def _normalize(s):
    """Нормализация строки для сравнения."""
    return s.lower().strip() if s else ""


def _format_isrc(isrc):
    """Очистка ISRC: убираем дефисы, приводим к верхнему регистру."""
    if not isrc:
        return ""
    return isrc.replace("-", "").upper().strip()


def _build_search_url(query, search_type="tracks"):
    """URL для поиска на Яндекс Музыке."""
    params = urlencode({"text": query, "type": search_type, "page": 0})
    return f"https://music.yandex.ru/handlers/search.jsx?{params}"


def _build_cover_url(cover_uri, size="1000x1000"):
    """Собирает полный URL обложки из coverUri Яндекса."""
    if not cover_uri:
        return None
    return f"https://{cover_uri.replace('%%', size)}"


def _match_track(track_data, artist, title):
    """Проверяет, похож ли найденный трек на искомый."""
    if not track_data:
        return False
    found_title = _normalize(track_data.get("title", ""))
    target_title = _normalize(title)
    # Простая проверка вхождения для гибкости
    if target_title not in found_title and found_title not in target_title:
        return False
    
    found_artists = [_normalize(a.get("name", "")) for a in track_data.get("artists", [])]
    target_artist = _normalize(artist)
    
    return any(target_artist in fa or fa in target_artist for fa in found_artists)


def _match_album(album_data, album_artist, album_title):
    """Проверяет, похож ли найденный альбом на искомый."""
    if not album_data:
        return False
    found_title = _normalize(album_data.get("title", ""))
    target_title = _normalize(album_title)
    
    if target_title not in found_title and found_title not in target_title:
        return False
    
    found_artists = [_normalize(a.get("name", "")) for a in album_data.get("artists", [])]
    target_artist = _normalize(album_artist)
    
    return any(target_artist in fa or fa in target_artist for fa in found_artists)


def _extract_cover_uri(album_data):
    """Достаёт URI обложки из данных альбома."""
    if not album_data:
        return None
    return (
        album_data.get("coverUri")
        or album_data.get("ogImage")
        or album_data.get("uri")
    )


# ═══════════════════════════════════════════════════════════════════════
#  Обработчик метаданных треков
# ═══════════════════════════════════════════════════════════════════════

def _handle_track_search(api, album, metadata, task_id, artist, title, isrc,
                         response, reply, error):
    """Callback: обрабатывает ответ поиска трека."""
    try:
        if error:
            api.logger.error(f"Yandex Music: ошибка поиска трека — {error}")
            return

        data = response.json() if response else None
        if not data:
            return

        tracks = data.get("tracks", {}).get("results", [])
        if not tracks:
            api.logger.debug(
                f"Yandex Music: ничего не найдено для "
                f"ISRC={isrc or '—'}, «{artist} — {title}»"
            )
            return

        matched = None
        if isrc:
            # Если есть ISRC, берем первый результат как наиболее релевантный
            matched = tracks
        else:
            for track_data in tracks:
                if _match_track(track_data, artist, title):
                    matched = track_data
                    break

        if not matched:
            return

        album_data = (matched.get("albums") or [{}])

        # --- ИСПРАВЛЕНО: Доступ к config через ["key"] с обработкой KeyError ---
        try:
            fetch_genre = api.plugin_config["fetch_genre"]
        except KeyError:
            fetch_genre = True

        if fetch_genre:
            genre = album_data.get("genre") or matched.get("genre")
            if genre:
                metadata["yandex_genre"] = genre

        try:
            fetch_explicit = api.plugin_config["fetch_explicit"]
        except KeyError:
            fetch_explicit = True

        if fetch_explicit:
            if matched.get("content_warning") == "explicit" or matched.get("explicit"):
                metadata["yandex_explicit"] = "true"

        try:
            fetch_lyrics = api.plugin_config["fetch_lyrics"]
        except KeyError:
            fetch_lyrics = True

        if fetch_lyrics:
            lyrics = matched.get("lyrics")
            if lyrics:
                if isinstance(lyrics, dict):
                    full = lyrics.get("full_lyrics")
                    if full:
                        metadata["lyrics"] = full
                elif isinstance(lyrics, str):
                    metadata["lyrics"] = lyrics

        api.logger.info(
            f"Yandex Music: теги добавлены для «{artist} — {title}»"
            f" (по ISRC: {'да' if isrc else 'нет'})"
        )

    finally:
        api.complete_album_task(album, task_id)


def process_track(api, track, metadata, track_node, release_node=None):
    """Процессор метаданных трека."""
    # --- ИСПРАВЛЕНО: Проверка enabled через try/except ---
    try:
        enabled = api.plugin_config["enabled"]
    except KeyError:
        enabled = True

    if not enabled:
        return

    title = metadata.get("title", "")
    artist = metadata.get("artist", "")

    if not title or not artist:
        return

    try:
        use_isrc = api.plugin_config["use_isrc"]
    except KeyError:
        use_isrc = True

    raw_isrc = metadata.get("isrc", "")
    formatted_isrc = _format_isrc(raw_isrc)

    if use_isrc and formatted_isrc:
        search_query = formatted_isrc
        search_label = f"ISRC:{formatted_isrc}"
    else:
        search_query = f"{artist} {title}"
        search_label = f"{artist} — {title}"

    task_id = f"ya_track_{search_label}"
    
    # Регистрируем задачу в очереди альбома
    api.add_album_task(
        track.album, task_id,
        f"Yandex Music: поиск {search_label}",
        timeout=15.0,
    )

    url = _build_search_url(search_query, "tracks")

    # ИСПРАВЛЕНИЕ: Убран set_album_task_request.
    # В Picard 3.x достаточно вызвать web_service.get_url.
    # Picard автоматически свяжет этот запрос с задачей task_id, созданной выше.
    api.web_service.get_url(
        url=url,
        handler=partial(
            _handle_track_search,
            api, track.album, metadata, task_id,
            artist, title,
            formatted_isrc if (use_isrc and formatted_isrc) else "",
        ),
        priority=True,
        important=False,
    )


# ═══════════════════════════════════════════════════════════════════════
#  Провайдер обложек
# ═══════════════════════════════════════════════════════════════════════

class YandexMusicCoverProvider(CoverArtProvider):
    """Провайдер обложек из Яндекс Музыки."""

    NAME = "Yandex Music"
    TITLE = t_("Yandex Music")

    def enabled(self):
        """Проверка активности провайдера."""
        # --- ИСПРАВЛЕНО: Корректный доступ к ConfigSection ---
        try:
            enabled = self.api.plugin_config["enabled"]
        except KeyError:
            enabled = True

        if not enabled:
            return False

        try:
            fetch_covers = self.api.plugin_config["fetch_cover"]
        except KeyError:
            fetch_covers = True

        if not fetch_covers:
            return False

        # Не загружаем, если обложка уже найдена
        if self.coverart.front_image_found:
            return False

        return True

    def queue_images(self):
        """Запускает асинхронный поиск альбома."""
        album_artist = (
            self.metadata.get("albumartist", "")
            or self.metadata.get("artist", "")
        )
        album_title = self.metadata.get("album", "")

        if not album_artist or not album_title:
            return CoverArtProvider.FINISHED

        formatted_isrc = ""
        try:
            use_isrc = self.api.plugin_config["use_isrc"]
        except KeyError:
            use_isrc = True

        if use_isrc:
            for track in self.album.tracks:
                if track.metadata:
                    raw_isrc = track.metadata.get("isrc", "")
                    formatted_isrc = _format_isrc(raw_isrc)
                    if formatted_isrc:
                        break

        if formatted_isrc:
            search_query = formatted_isrc
            search_type = "tracks"
        else:
            search_query = f"{album_artist} {album_title}"
            search_type = "albums"

        url = _build_search_url(search_query, search_type)
        self.api.logger.debug(
            f"Yandex Music: поиск обложки для «{search_query}» "
            f"(тип: {search_type})"
        )

        # Для CoverArtProvider запросы ставятся в очередь автоматически через queue_put внутри хендлера
        self.api.web_service.get_url(
            url=url,
            handler=partial(self._handle_cover_search, formatted_isrc),
            priority=True,
            important=False,
        )

    def _handle_cover_search(self, isrc, response, reply, error):
        """Callback: обрабатывает ответ поиска и ставит обложку в очередь."""
        try:
            if error:
                self.api.logger.error(
                    f"Yandex Music: ошибка поиска обложки — {error}"
                )
                return

            data = response.json() if response else None
            if not data:
                return

            album_artist = (
                self.metadata.get("albumartist", "")
                or self.metadata.get("artist", "")
            )
            album_title = self.metadata.get("album", "")

            matched_album = None

            if isrc:
                tracks = data.get("tracks", {}).get("results", [])
                for track_data in tracks:
                    track_albums = track_data.get("albums", [])
                    if track_albums:
                        for ta in track_albums:
                            if _match_album(ta, album_artist, album_title):
                                matched_album = ta
                                break
                        if not matched_album:
                            matched_album = track_albums
                        break
            else:
                albums = data.get("albums", {}).get("results", [])
                for album_data in albums:
                    if _match_album(album_data, album_artist, album_title):
                        matched_album = album_data
                        break

                if not matched_album:
                    tracks = data.get("tracks", {}).get("results", [])
                    for track_data in tracks:
                        for ta in track_data.get("albums", []):
                            if _match_album(ta, album_artist, album_title):
                                matched_album = ta
                                break
                        if matched_album:
                            break

            if not matched_album:
                return

            cover_uri = _extract_cover_uri(matched_album)
            if not cover_uri:
                return

            cover_url = _build_cover_url(cover_uri, "1000x1000")
            if cover_url:
                self.api.logger.info(
                    f"Yandex Music: обложка найдена — {cover_url}"
                )
                self.queue_put(CoverArtImage(cover_url))

        except Exception as e:
            self.api.logger.error(
                f"Yandex Music: ошибка обработки обложки — {e}"
            )
        finally:
            self.next_in_queue()


# ═══════════════════════════════════════════════════════════════════════
#  Страница настроек
# ═══════════════════════════════════════════════════════════════════════

class YandexMusicOptionsPage(OptionsPage):
    NAME = "yandex_music"
    TITLE = t_("Yandex Music")
    PARENT = "plugins"

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)

        self.enabled_cb = QCheckBox("Получать метаданные из Яндекс Музыки")
        layout.addWidget(self.enabled_cb)

        layout.addWidget(QLabel(
            "Токен Яндекс Музыки (опционально):"
        ))
        self.token_input = QLineEdit()
        self.token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_input.setPlaceholderText("OAuth-токен Яндекс Музыки")
        layout.addWidget(self.token_input)

        self.use_isrc_cb = QCheckBox(
            "Искать по ISRC в первую очередь"
        )
        layout.addWidget(self.use_isrc_cb)

        layout.addWidget(QLabel("Что загружать:"))
        self.fetch_lyrics_cb = QCheckBox("Тексты песен")
        layout.addWidget(self.fetch_lyrics_cb)
        self.fetch_genre_cb = QCheckBox("Жанр")
        layout.addWidget(self.fetch_genre_cb)
        self.fetch_explicit_cb = QCheckBox("Метку explicit (18+)")
        layout.addWidget(self.fetch_explicit_cb)
        self.fetch_covers_cb = QCheckBox("Обложки альбомов")
        layout.addWidget(self.fetch_covers_cb)

        layout.addStretch()

    def load(self):
        try:
            self.enabled_cb.setChecked(self.api.plugin_config["enabled"])
        except KeyError:
            self.enabled_cb.setChecked(True)
            
        # Для токена используем .get(), так как это строковое значение, а не флаг
        self.token_input.setText(self.api.plugin_config.get("token", ""))
        
        try:
            self.use_isrc_cb.setChecked(self.api.plugin_config["use_isrc"])
        except KeyError:
            self.use_isrc_cb.setChecked(True)
            
        try:
            self.fetch_lyrics_cb.setChecked(self.api.plugin_config["fetch_lyrics"])
        except KeyError:
            self.fetch_lyrics_cb.setChecked(True)
            
        try:
            self.fetch_genre_cb.setChecked(self.api.plugin_config["fetch_genre"])
        except KeyError:
            self.fetch_genre_cb.setChecked(True)
            
        try:
            self.fetch_explicit_cb.setChecked(self.api.plugin_config["fetch_explicit"])
        except KeyError:
            self.fetch_explicit_cb.setChecked(True)
            
        try:
            self.fetch_covers_cb.setChecked(self.api.plugin_config["fetch_cover"])
        except KeyError:
            self.fetch_covers_cb.setChecked(True)

    def save(self):
        self.api.plugin_config["enabled"] = self.enabled_cb.isChecked()
        self.api.plugin_config["token"] = self.token_input.text()
        self.api.plugin_config["use_isrc"] = self.use_isrc_cb.isChecked()
        self.api.plugin_config["fetch_lyrics"] = self.fetch_lyrics_cb.isChecked()
        self.api.plugin_config["fetch_genre"] = self.fetch_genre_cb.isChecked()
        self.api.plugin_config["fetch_explicit"] = self.fetch_explicit_cb.isChecked()
        self.api.plugin_config["fetch_cover"] = self.fetch_covers_cb.isChecked()


# ═══════════════════════════════════════════════════════════════════════
#  Точка входа
# ═══════════════════════════════════════════════════════════════════════

def enable(api):
    # --- ИСПРАВЛЕНО: Регистрация всех опций обязательна для Picard 3.x ---
    # Без этого plugin_config не будет содержать ключей, и возникнет KeyError
    api.plugin_config.register_option("enabled", True)
    api.plugin_config.register_option("token", "")
    api.plugin_config.register_option("use_isrc", True)
    api.plugin_config.register_option("fetch_genre", True)
    api.plugin_config.register_option("fetch_explicit", True)
    api.plugin_config.register_option("fetch_lyrics", True)
    api.plugin_config.register_option("fetch_cover", True)

    api.register_options_page(YandexMusicOptionsPage)
    api.register_cover_art_provider(YandexMusicCoverProvider)
    
    # Приоритет -50 означает, что плагин сработает после основных процессоров Picard
    api.register_track_metadata_processor(process_track, priority=-50)

    api.logger.info("Yandex Music Metadata plugin v0.3 loaded successfully")
