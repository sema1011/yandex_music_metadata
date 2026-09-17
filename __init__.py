import json
import threading
import urllib.request
from functools import partial
from urllib.parse import urlencode

from PyQt6.QtCore import QObject, pyqtSignal
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
#  Заголовки для запросов к API Яндекс Музыки
# ═══════════════════════════════════════════════════════════════════════

YANDEX_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "identity",
}


# ═══════════════════════════════════════════════════════════════════════
#  Глобальное хранилище ссылок на активные fetcher'ы (защита от GC)
# ═══════════════════════════════════════════════════════════════════════

_active_fetchers = set()


# ═══════════════════════════════════════════════════════════════════════
#  Асинхронный HTTP-клиент на сигналах Qt (потокобезопасный)
# ═══════════════════════════════════════════════════════════════════════

class _AsyncFetcher(QObject):
    fetched = pyqtSignal(object, object)

    def __init__(self, url, is_json=True, timeout=15, on_success=None, on_error=None):
        super().__init__()
        self._url = url
        self._is_json = is_json
        self._timeout = timeout
        self._on_success = on_success
        self._on_error = on_error
        self.fetched.connect(self._dispatch)

    def start(self):
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            if self._is_json:
                headers = YANDEX_HEADERS
            else:
                headers = {
                    "User-Agent": YANDEX_HEADERS["User-Agent"],
                    "Accept": "image/*",
                    "Accept-Encoding": "identity",
                }

            req = urllib.request.Request(self._url, headers=headers)
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                status = resp.status
                raw = resp.read()

                if self._is_json:
                    text = raw.decode("utf-8", errors="replace")
                    if not text.strip():
                        self.fetched.emit(None, f"Пустой ответ (HTTP {status})")
                        return
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError as e:
                        snippet = text[:300].replace("\n", "\\n")
                        self.fetched.emit(
                            None,
                            f"Ошибка JSON: {e} | HTTP {status} | ответ: {snippet}"
                        )
                        return
                    self.fetched.emit(data, None)
                else:
                    if not raw:
                        self.fetched.emit(None, f"Пустые данные (HTTP {status})")
                        return
                    self.fetched.emit(raw, None)
        except urllib.error.HTTPError as e:
            self.fetched.emit(None, f"HTTP {e.code}: {e.reason}")
        except Exception as e:
            self.fetched.emit(None, f"{type(e).__name__}: {e}")

    def _dispatch(self, data, error):
        try:
            if error is not None:
                if self._on_error:
                    self._on_error(error)
            else:
                if self._on_success:
                    self._on_success(data)
        finally:
            _active_fetchers.discard(self)


def _fetch_json_async(url, on_success, on_error, timeout=15):
    fetcher = _AsyncFetcher(
        url, is_json=True, timeout=timeout,
        on_success=on_success, on_error=on_error,
    )
    _active_fetchers.add(fetcher)
    fetcher.start()
    return fetcher


def _fetch_bytes_async(url, on_success, on_error, timeout=30):
    fetcher = _AsyncFetcher(
        url, is_json=False, timeout=timeout,
        on_success=on_success, on_error=on_error,
    )
    _active_fetchers.add(fetcher)
    fetcher.start()
    return fetcher


# ═══════════════════════════════════════════════════════════════════════
#  Вспомогательные функции
# ═══════════════════════════════════════════════════════════════════════

def _normalize(s):
    return s.lower().strip() if s else ""


def _format_isrc(isrc):
    if not isrc:
        return ""
    return isrc.replace("-", "").upper().strip()


def _build_search_url(query, search_type="track"):
    """URL для поиска через api.music.yandex.net.

    ВАЖНО: путь /search (БЕЗ префикса /api).
    Ответ обёрнут в поле 'result'.
    """
    params = urlencode({
        "text": query,
        "type": search_type,
        "page": 0,
        "nocorrect": "false",
    })
    return f"https://api.music.yandex.net/search?{params}"


def _build_cover_url(cover_uri, size="1000x1000"):
    if not cover_uri:
        return None
    return f"https://{cover_uri.replace('%%', size)}"


def _extract_result(data):
    """API Яндекс Музыки оборачивает результат в поле 'result'."""
    if not data:
        return {}
    return data.get("result", data)


def _match_track(track_data, artist, title):
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
    if not album_data:
        return None
    cover = album_data.get("cover")
    if isinstance(cover, dict):
        uri = cover.get("uri")
        if uri:
            return uri
    return (
        album_data.get("coverUri")
        or album_data.get("ogImage")
        or album_data.get("uri")
    )


def _cfg(api, key, default):
    try:
        return api.plugin_config[key]
    except KeyError:
        return default


# ═══════════════════════════════════════════════════════════════════════
#  Обработчик метаданных треков
# ═══════════════════════════════════════════════════════════════════════

def _handle_track_search_result(api, album, metadata, task_id, artist, title,
                                 isrc, data):
    try:
        if not data:
            return

        result = _extract_result(data)
        tracks = result.get("tracks", {}).get("results", [])
        if not tracks:
            api.logger.debug(
                f"Yandex Music: ничего не найдено для "
                f"ISRC={isrc or '—'}, «{artist} — {title}»"
            )
            return

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

        album_data = (matched.get("albums") or [{}])[0]

        if _cfg(api, "fetch_genre", True):
            genre = album_data.get("genre") or matched.get("genre")
            if genre:
                metadata["yandex_genre"] = genre

        if _cfg(api, "fetch_explicit", True):
            if matched.get("content_warning") == "explicit" or matched.get("explicit"):
                metadata["yandex_explicit"] = "true"

        if _cfg(api, "fetch_lyrics", True):
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


def _handle_track_search_error(api, album, task_id, artist, title, error):
    try:
        api.logger.error(f"Yandex Music: ошибка поиска трека — {error}")
    finally:
        api.complete_album_task(album, task_id)


def process_track(api, track, metadata, track_node, release_node=None):
    if not _cfg(api, "enabled", True):
        return

    title = metadata.get("title", "")
    artist = metadata.get("artist", "")

    if not title or not artist:
        return

    use_isrc = _cfg(api, "use_isrc", True)
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

    url = _build_search_url(search_query, "track")
    isrc_for_handler = formatted_isrc if (use_isrc and formatted_isrc) else ""

    _fetch_json_async(
        url,
        on_success=partial(
            _handle_track_search_result,
            api, track.album, metadata, task_id,
            artist, title, isrc_for_handler,
        ),
        on_error=partial(
            _handle_track_search_error,
            api, track.album, task_id, artist, title,
        ),
    )


# ═══════════════════════════════════════════════════════════════════════
#  Провайдер обложек
# ═══════════════════════════════════════════════════════════════════════

class YandexMusicCoverProvider(CoverArtProvider):
    NAME = "Yandex Music"
    TITLE = t_("Yandex Music")

    def enabled(self):
        if not _cfg(self.api, "enabled", True):
            return False
        if not _cfg(self.api, "fetch_cover", True):
            return False
        if self.coverart.front_image_found:
            return False
        return True

    def queue_images(self):
        album_artist = (
            self.metadata.get("albumartist", "")
            or self.metadata.get("artist", "")
        )
        album_title = self.metadata.get("album", "")

        if not album_artist or not album_title:
            return CoverArtProvider.FINISHED

        formatted_isrc = ""
        use_isrc = _cfg(self.api, "use_isrc", True)

        if use_isrc:
            for track in self.album.tracks:
                if track.metadata:
                    raw_isrc = track.metadata.get("isrc", "")
                    formatted_isrc = _format_isrc(raw_isrc)
                    if formatted_isrc:
                        break

        if formatted_isrc:
            search_query = formatted_isrc
            search_type = "track"
        else:
            search_query = f"{album_artist} {album_title}"
            search_type = "album"

        url = _build_search_url(search_query, search_type)
        self.api.logger.debug(
            f"Yandex Music: поиск обложки для «{search_query}» "
            f"(тип: {search_type})"
        )

        _fetch_json_async(
            url,
            on_success=partial(self._handle_cover_search, formatted_isrc),
            on_error=self._handle_cover_error,
        )

    def _handle_cover_search(self, isrc, data):
        try:
            if not data:
                self.next_in_queue()
                return

            result = _extract_result(data)

            album_artist = (
                self.metadata.get("albumartist", "")
                or self.metadata.get("artist", "")
            )
            album_title = self.metadata.get("album", "")

            matched_album = None

            if isrc:
                tracks = result.get("tracks", {}).get("results", [])
                for track_data in tracks:
                    track_albums = track_data.get("albums", [])
                    if track_albums:
                        for ta in track_albums:
                            if _match_album(ta, album_artist, album_title):
                                matched_album = ta
                                break
                        if not matched_album:
                            matched_album = track_albums[0]
                        break
            else:
                albums = result.get("albums", {}).get("results", [])
                for album_data in albums:
                    if _match_album(album_data, album_artist, album_title):
                        matched_album = album_data
                        break

                if not matched_album:
                    tracks = result.get("tracks", {}).get("results", [])
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
                self.next_in_queue()
                return

            cover_uri = _extract_cover_uri(matched_album)
            if not cover_uri:
                self.next_in_queue()
                return

            cover_url = _build_cover_url(cover_uri, "1000x1000")
            if cover_url:
                self.api.logger.info(
                    f"Yandex Music: обложка найдена — {cover_url}"
                )
                _fetch_bytes_async(
                    cover_url,
                    on_success=self._handle_cover_download,
                    on_error=self._handle_cover_error,
                )
            else:
                self.next_in_queue()

        except Exception as e:
            self.api.logger.error(
                f"Yandex Music: ошибка обработки обложки — {e}"
            )
            self.next_in_queue()

    def _handle_cover_download(self, image_bytes):
        try:
            if image_bytes:
                self.queue_put(CoverArtImage(image_bytes))
        except Exception as e:
            self.api.logger.error(
                f"Yandex Music: ошибка добавления обложки — {e}"
            )
        finally:
            self.next_in_queue()

    def _handle_cover_error(self, error):
        try:
            self.api.logger.error(
                f"Yandex Music: ошибка загрузки обложки — {error}"
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

        layout.addWidget(QLabel("Токен Яндекс Музыки (опционально):"))
        self.token_input = QLineEdit()
        self.token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_input.setPlaceholderText("OAuth-токен Яндекс Музыки")
        layout.addWidget(self.token_input)

        self.use_isrc_cb = QCheckBox("Искать по ISRC в первую очередь")
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
        self.enabled_cb.setChecked(_cfg(self.api, "enabled", True))
        self.token_input.setText(_cfg(self.api, "token", ""))
        self.use_isrc_cb.setChecked(_cfg(self.api, "use_isrc", True))
        self.fetch_lyrics_cb.setChecked(_cfg(self.api, "fetch_lyrics", True))
        self.fetch_genre_cb.setChecked(_cfg(self.api, "fetch_genre", True))
        self.fetch_explicit_cb.setChecked(_cfg(self.api, "fetch_explicit", True))
        self.fetch_covers_cb.setChecked(_cfg(self.api, "fetch_cover", True))

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
    api.plugin_config.register_option("enabled", True)
    api.plugin_config.register_option("token", "")
    api.plugin_config.register_option("use_isrc", True)
    api.plugin_config.register_option("fetch_genre", True)
    api.plugin_config.register_option("fetch_explicit", True)
    api.plugin_config.register_option("fetch_lyrics", True)
    api.plugin_config.register_option("fetch_cover", True)

    api.register_options_page(YandexMusicOptionsPage)
    api.register_cover_art_provider(YandexMusicCoverProvider)
    api.register_track_metadata_processor(process_track, priority=-50)

    api.logger.info("Yandex Music Metadata plugin v0.9 loaded")
