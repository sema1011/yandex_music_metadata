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
    """URL для поиска на Яндекс Музыке через публичный эндпоинт."""
    params = urlencode({"text": query, "type": search_type, "page": 0})
    return f"https://music.yandex.ru/handlers/search.jsx?{params}"


def _build_cover_url(cover_uri, size="1000x1000"):
    """Собирает полный URL обложки из coverUri Яндекса.

    Яндекс отдаёт URI вида:
        avatars.yandex.net/get-music-content/12345/abc.p.123/%%
    Символы %% заменяются на размер, например 1000x1000.
    """
    if not cover_uri:
        return None
    return f"https://{cover_uri.replace('%%', size)}"


def _match_track(track_data, artist, title):
    """Проверяет, похож ли найденный трек на искомый."""
    if not track_data:
        return False
    found_title = _normalize(track_data.get("title", ""))
    target_title = _normalize(title)
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
    """Достаёт URI обложки из данных альбома. Пробует несколько полей."""
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

        # Если искали по ISRC — берём первый результат.
        # Иначе — ищем точное совпадение по названию и исполнителю.
        matched = None
        if isrc:
            matched = tracks[0]
        else:
            for track_data in tracks:
                if _match_track(track_data, artist, title):
                    matched = track_data
                    break

        if not matched:
            api.logger.debug(
                f"Yandex Music: нет точного совпадения для «{artist} — {title}»"
            )
            return

        # Данные альбома из найденного трека
        album_data = (matched.get("albums") or [{}])[0]

        # Жанр
        if api.plugin_config.get("fetch_genre", True):
            genre = album_data.get("genre") or matched.get("genre")
            if genre:
                metadata["yandex_genre"] = genre

        # Метка explicit
        if api.plugin_config.get("fetch_explicit", True):
            if matched.get("content_warning") == "explicit" or matched.get("explicit"):
                metadata["yandex_explicit"] = "true"

        # Текст песни
        if api.plugin_config.get("fetch_lyrics", True):
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
    """Процессор метаданных трека: ищет на Яндекс Музыке и дополняет теги."""
    if not api.plugin_config.get("enabled", True):
        return

    title = metadata.get("title", "")
    artist = metadata.get("artist", "")

    if not title or not artist:
        return

    # Определяем стратегию поиска
    use_isrc = api.plugin_config.get("use_isrc", True)
    raw_isrc = metadata.get("isrc", "")
    formatted_isrc = _format_isrc(raw_isrc)

    if use_isrc and formatted_isrc:
        search_query = formatted_isrc
        search_label = f"ISRC:{formatted_isrc}"
    else:
        search_query = f"{artist} {title}"
        search_label = f"{artist} — {title}"

    task_id = f"ya_track_{search_label}"
    api.add_album_task(
        track.album, task_id,
        f"Yandex Music: поиск {search_label}",
        timeout=15.0,
    )

    url = _build_search_url(search_query, "tracks")

    request = api.web_service.get_url(
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
    api.set_album_task_request(track.album, task_id, request)


# ═══════════════════════════════════════════════════════════════════════
#  Провайдер обложек
# ═══════════════════════════════════════════════════════════════════════

class YandexMusicCoverProvider(CoverArtProvider):
    """Провайдер обложек из Яндекс Музыки.

    Ищет альбом по ISRC (если есть) или по исполнителю + названию,
    затем загружает обложку через coverUri.
    """

    NAME = "Yandex Music"
    TITLE = t_("Yandex Music")

    def enabled(self):
        """Провайдер активен, если включён в настройках
        и обложка ещё не найдена другими провайдерами."""
        if not self.api.plugin_config.get("enabled", True):
            return False
        if not self.api.plugin_config.get("fetch_covers", True):
            return False
        return super().enabled() and not self.coverart.front_image_found

    def queue_images(self):
        """Запускает асинхронный поиск альбома на Яндекс Музыке."""
        album_artist = (
            self.metadata.get("albumartist", "")
            or self.metadata.get("artist", "")
        )
        album_title = self.metadata.get("album", "")

        if not album_artist or not album_title:
            return CoverArtProvider.FINISHED

        # Пытаемся найти ISRC у первого трека альбома
        formatted_isrc = ""
        use_isrc = self.api.plugin_config.get("use_isrc", True)
        if use_isrc:
            for track in self.album.tracks:
                if track.metadata:
                    raw_isrc = track.metadata.get("isrc", "")
                    formatted_isrc = _format_isrc(raw_isrc)
                    if formatted_isrc:
                        break

        # Стратегия поиска
        if formatted_isrc:
            search_query = formatted_isrc
            search_type = "tracks"  # ISRC ищет треки, из них достаём альбом
        else:
            search_query = f"{album_artist} {album_title}"
            search_type = "albums"

        url = _build_search_url(search_query, search_type)
        self.api.logger.debug(
            f"Yandex Music: поиск обложки для «{search_query}» "
            f"(тип: {search_type})"
        )

        self.api.web_service.get_url(
            url=url,
            handler=partial(self._handle_cover_search, formatted_isrc),
            priority=True,
            important=False,
        )
        # Не возвращаем FINISHED — ждём асинхронный ответ,
        # после чего вызовем self.next_in_queue().

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

            # Если искали по ISRC — берём альбом из первого найденного трека
            if isrc:
                tracks = data.get("tracks", {}).get("results", [])
                for track_data in tracks:
                    track_albums = track_data.get("albums", [])
                    if track_albums:
                        # Проверяем совпадение альбома
                        for ta in track_albums:
                            if _match_album(ta, album_artist, album_title):
                                matched_album = ta
                                break
                        if not matched_album:
                            # Берём первый альбом первого трека
                            matched_album = track_albums[0]
                        break
            else:
                # Искали по названию альбома — берём из результатов albums
                albums = data.get("albums", {}).get("results", [])
                for album_data in albums:
                    if _match_album(album_data, album_artist, album_title):
                        matched_album = album_data
                        break

                # Если не нашли в albums, пробуем в tracks → albums
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
                self.api.logger.debug(
                    f"Yandex Music: альбом не найден для "
                    f"«{album_artist} — {album_title}»"
                )
                return

            cover_uri = _extract_cover_uri(matched_album)
            if not cover_uri:
                self.api.logger.debug(
                    f"Yandex Music: у альбома «{matched_album.get('title', '?')}» "
                    f"нет обложки"
                )
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
            "Токен Яндекс Музыки (опционально, для текстов и полного доступа):"
        ))
        self.token_input = QLineEdit()
        self.token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_input.setPlaceholderText("OAuth-токен Яндекс Музыки")
        layout.addWidget(self.token_input)

        self.use_isrc_cb = QCheckBox(
            "Искать по ISRC в первую очередь (точнее, но не у всех треков есть ISRC)"
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
        self.enabled_cb.setChecked(self.api.plugin_config.get("enabled", True))
        self.token_input.setText(self.api.plugin_config.get("token", ""))
        self.use_isrc_cb.setChecked(self.api.plugin_config.get("use_isrc", True))
        self.fetch_lyrics_cb.setChecked(self.api.plugin_config.get("fetch_lyrics", True))
        self.fetch_genre_cb.setChecked(self.api.plugin_config.get("fetch_genre", True))
        self.fetch_explicit_cb.setChecked(
            self.api.plugin_config.get("fetch_explicit", True)
        )
        self.fetch_covers_cb.setChecked(
            self.api.plugin_config.get("fetch_covers", True)
        )

    def save(self):
        self.api.plugin_config["enabled"] = self.enabled_cb.isChecked()
        self.api.plugin_config["token"] = self.token_input.text()
        self.api.plugin_config["use_isrc"] = self.use_isrc_cb.isChecked()
        self.api.plugin_config["fetch_lyrics"] = self.fetch_lyrics_cb.isChecked()
        self.api.plugin_config["fetch_genre"] = self.fetch_genre_cb.isChecked()
        self.api.plugin_config["fetch_explicit"] = self.fetch_explicit_cb.isChecked()
        self.api.plugin_config["fetch_covers"] = self.fetch_covers_cb.isChecked()


# ═══════════════════════════════════════════════════════════════════════
#  Точка входа
# ═══════════════════════════════════════════════════════════════════════

def enable(api):
    # Регистрируем опции с значениями по умолчанию
    api.plugin_config.register_option("enabled", True)
    api.plugin_config.register_option("token", "")
    api.plugin_config.register_option("use_isrc", True)
    api.plugin_config.register_option("fetch_lyrics", True)
    api.plugin_config.register_option("fetch_genre", True)
    api.plugin_config.register_option("fetch_explicit", True)
    api.plugin_config.register_option("fetch_covers", True)

    # Процессор метаданных треков (низкий приоритет — после основных плагинов)
    api.register_track_metadata_processor(process_track, priority=-50)

    # Провайдер обложек
    api.register_cover_art_provider(YandexMusicCoverProvider)

    # Страница настроек
    api.register_options_page(YandexMusicOptionsPage)

    api.logger.info("Yandex Music Metadata plugin v0.2 loaded")
