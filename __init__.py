import json
import threading
import time
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

# ── Заголовки для запросов к API Яндекс Музыки ──────────────────────────

_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "identity",
}


def _build_headers(token=""):
    """Собрать заголовки с опциональным OAuth-токеном."""
    headers = dict(_BASE_HEADERS)
    if token:
        headers["Authorization"] = f"OAuth {token}"
    return headers

# ── Rate limiter (защита от HTTP 429) ───────────────────────────────────

_rate_lock = threading.Lock()
_last_request_time = 0.0
_REQUEST_INTERVAL = 0.3
_last_429_time = 0.0
_RATE_LIMIT_BACKOFF = 5.0  # секунды ожидания после 429


def _wait_for_rate_limit():
    global _last_request_time, _last_429_time
    with _rate_lock:
        # Адаптивный backoff после HTTP 429
        if _last_429_time > 0:
            elapsed = time.monotonic() - _last_429_time
            if elapsed < _RATE_LIMIT_BACKOFF:
                time.sleep(_RATE_LIMIT_BACKOFF - elapsed)
                _last_429_time = 0.0
                return

        elapsed = time.monotonic() - _last_request_time
        wait = _REQUEST_INTERVAL - elapsed
        if wait > 0:
            time.sleep(wait)
        _last_request_time = time.monotonic()


def _record_rate_limit_hit():
    global _last_429_time
    with _rate_lock:
        _last_429_time = time.monotonic()


# ── Синхронный HTTP (для обложек) ───────────────────────────────────────

def _fetch_json_sync(url, timeout=10, token=""):
    _wait_for_rate_limit()
    req = urllib.request.Request(url, headers=_build_headers(token))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status == 429:
            _record_rate_limit_hit()
            raise urllib.error.HTTPError(url, 429, "Rate limited", {}, None)
        raw = resp.read()
    text = raw.decode("utf-8", errors="replace")
    return json.loads(text) if text.strip() else None


# ── Асинхронный HTTP (для треков) ───────────────────────────────────────

_active_fetchers = set()
_active_fetchers_lock = threading.Lock()


class _AsyncFetcher(QObject):
    fetched = pyqtSignal(object, object)

    def __init__(self, url, timeout=15, on_success=None, on_error=None, token=""):
        super().__init__()
        self._url = url
        self._timeout = timeout
        self._on_success = on_success
        self._on_error = on_error
        self._token = token
        self.fetched.connect(self._dispatch)

    def start(self):
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self):
        try:
            _wait_for_rate_limit()
            req = urllib.request.Request(self._url, headers=_build_headers(self._token))
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                if resp.status == 429:
                    _record_rate_limit_hit()
                    self.fetched.emit(None, "HTTP 429: Rate limited")
                    return
                raw = resp.read()
            text = raw.decode("utf-8", errors="replace")
            if not text.strip():
                self.fetched.emit(None, "Пустой ответ")
                return
            data = json.loads(text)
            self.fetched.emit(data, None)
        except urllib.error.HTTPError as e:
            self.fetched.emit(None, f"HTTP {e.code}: {e.reason}")
        except Exception as e:
            self.fetched.emit(None, f"{type(e).__name__}: {e}")

    def _dispatch(self, data, error):
        try:
            if error is not None:
                if self._on_error:
                    self._on_error(error)
            elif self._on_success:
                self._on_success(data)
        finally:
            with _active_fetchers_lock:
                _active_fetchers.discard(self)


def _fetch_json_async(url, on_success, on_error, timeout=15, token=""):
    fetcher = _AsyncFetcher(url, timeout=timeout,
                            on_success=on_success, on_error=on_error,
                            token=token)
    with _active_fetchers_lock:
        _active_fetchers.add(fetcher)
    fetcher.start()


# ── Вспомогательные функции ─────────────────────────────────────────────

def _normalize(s):
    return s.lower().strip().replace("ё", "е") if s else ""


def _format_isrc(isrc):
    return isrc.replace("-", "").upper().strip() if isrc else ""


def _build_search_url(query, search_type="track"):
    params = urlencode({
        "text": query, "type": search_type,
        "page": 0, "nocorrect": "false",
    })
    return f"https://api.music.yandex.net/search?{params}"


def _build_cover_url(cover_uri, size="1000x1000"):
    return f"https://{cover_uri.replace('%%', size)}" if cover_uri else None


def _extract_result(data):
    return data.get("result", data) if data else {}


def _match_names(found_list, target):
    target = _normalize(target)
    for f in found_list:
        nf = _normalize(f)
        if not nf:
            continue
        if target == nf or target in nf or nf in target:
            return True
    return False


def _match_track(track_data, artist, title):
    if not track_data:
        return False
    ft, tt = _normalize(track_data.get("title", "")), _normalize(title)
    if tt not in ft and ft not in tt:
        return False
    artists = [_normalize(a.get("name", "")) for a in track_data.get("artists", [])]
    return _match_names(artists, artist)


def _match_album(album_data, album_artist, album_title):
    if not album_data:
        return False
    ft, tt = _normalize(album_data.get("title", "")), _normalize(album_title)
    if tt not in ft and ft not in tt:
        return False
    artists = [_normalize(a.get("name", "")) for a in album_data.get("artists", [])]
    return _match_names(artists, album_artist)


def _extract_cover_uri(album_data):
    if not album_data:
        return None
    cover = album_data.get("cover")
    if isinstance(cover, dict) and cover.get("uri"):
        return cover["uri"]
    return album_data.get("coverUri") or album_data.get("ogImage")


def _cfg(api, key, default):
    try:
        return api.plugin_config[key]
    except KeyError:
        return default


# ── Поиск альбома в ответе API ──────────────────────────────────────────

def _find_album(result, isrc, album_artist, album_title):
    if isrc:
        for track in result.get("tracks", {}).get("results", []):
            albums = track.get("albums") or []
            if not albums:
                continue
            for a in albums:
                if _match_album(a, album_artist, album_title):
                    return a
            return albums[0]
    else:
        for a in result.get("albums", {}).get("results", []):
            if _match_album(a, album_artist, album_title):
                return a
        for track in result.get("tracks", {}).get("results", []):
            for a in track.get("albums") or []:
                if _match_album(a, album_artist, album_title):
                    return a
    return None


# ── Обработчик треков ───────────────────────────────────────────────────

def _handle_track_result(api, album, metadata, task_id, artist, title, isrc, data):
    try:
        if not data:
            return
        tracks = _extract_result(data).get("tracks", {}).get("results", [])
        if not tracks:
            api.logger.debug(
                f"Yandex Music: не найдено — ISRC={isrc or '—'}, «{artist} — {title}»")
            return

        matched = tracks[0] if isrc else None
        if not matched:
            for t in tracks:
                if _match_track(t, artist, title):
                    matched = t
                    break
        if not matched:
            api.logger.debug(f"Yandex Music: нет совпадения — «{artist} — {title}»")
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
            if isinstance(lyrics, dict):
                full = lyrics.get("full_lyrics")
                if full:
                    metadata["lyrics"] = full
            elif isinstance(lyrics, str):
                metadata["lyrics"] = lyrics

        api.logger.info(
            f"Yandex Music: теги добавлены — «{artist} — {title}»"
            f" (ISRC: {'да' if isrc else 'нет'})")
    finally:
        api.complete_album_task(album, task_id)


def _handle_track_error(api, album, task_id, error):
    api.logger.error(f"Yandex Music: ошибка поиска трека — {error}")
    api.complete_album_task(album, task_id)


def process_track(api, track, metadata, track_node):
    if not _cfg(api, "enabled", True):
        return

    title = metadata.get("title", "")
    artist = metadata.get("artist", "")
    if not title or not artist:
        return

    use_isrc = _cfg(api, "use_isrc", True)
    isrc = _format_isrc(metadata.get("isrc", "")) if use_isrc else ""
    token = _cfg(api, "token", "")

    if isrc:
        query, label = isrc, f"ISRC:{isrc}"
    else:
        query, label = f"{artist} {title}", f"{artist} — {title}"

    task_id = f"ya_track_{label}"
    api.add_album_task(track.album, task_id, f"Yandex Music: поиск {label}")

    _fetch_json_async(
        _build_search_url(query, "track"),
        on_success=partial(_handle_track_result, api, track.album,
                            metadata, task_id, artist, title, isrc),
        on_error=partial(_handle_track_error, api, track.album, task_id),
        token=token,
    )


# ── Провайдер обложек ───────────────────────────────────────────────────

class YandexMusicCoverProvider(CoverArtProvider):
    NAME = "Yandex Music"
    TITLE = t_("Yandex Music")

    def enabled(self):
        return (_cfg(self.api, "enabled", True)
                and _cfg(self.api, "fetch_cover", True)
                and not self.coverart.front_image_found)

    def queue_images(self):
        album_artist = self.metadata.get("albumartist", "") or self.metadata.get("artist", "")
        album_title = self.metadata.get("album", "")
        if not album_artist or not album_title:
            return 0

        isrc = ""
        if _cfg(self.api, "use_isrc", True):
            for track in self.album.tracks:
                if track.metadata:
                    isrc = _format_isrc(track.metadata.get("isrc", ""))
                    if isrc:
                        break

        query = isrc if isrc else f"{album_artist} {album_title}"
        search_type = "track" if isrc else "album"

        self.api.logger.debug(f"Yandex Music: поиск обложки «{query}» ({search_type})")

        token = _cfg(self.api, "token", "")

        try:
            data = _fetch_json_sync(_build_search_url(query, search_type), token=token)
        except Exception as e:
            self.api.logger.error(f"Yandex Music: ошибка поиска обложки — {e}")
            return 0

        if not data:
            return 0

        matched = _find_album(_extract_result(data), isrc, album_artist, album_title)
        if not matched:
            self.api.logger.debug(
                f"Yandex Music: альбом не найден — «{album_artist} — {album_title}»")
            return 0

        cover_url = _build_cover_url(_extract_cover_uri(matched))
        if cover_url:
            self.api.logger.info(f"Yandex Music: обложка найдена — {cover_url}")
            self.queue_put(CoverArtImage(cover_url))

        return 0


# ── Страница настроек ───────────────────────────────────────────────────

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
        self.fetch_genre_cb = QCheckBox("Жанр")
        self.fetch_explicit_cb = QCheckBox("Метку explicit (18+)")
        self.fetch_covers_cb = QCheckBox("Обложки альбомов")
        for cb in (self.fetch_lyrics_cb, self.fetch_genre_cb,
                   self.fetch_explicit_cb, self.fetch_covers_cb):
            layout.addWidget(cb)

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


# ── Точка входа ────────────────────────────────────────────────────────

def enable(api):
    for key, val in [("enabled", True), ("token", ""), ("use_isrc", True),
                     ("fetch_genre", True), ("fetch_explicit", True),
                     ("fetch_lyrics", True), ("fetch_cover", True)]:
        api.plugin_config.register_option(key, val)

    api.register_options_page(YandexMusicOptionsPage)
    api.register_cover_art_provider(YandexMusicCoverProvider)
    api.register_track_metadata_processor(process_track, priority=-50)
    api.logger.info("Yandex Music Metadata plugin v1.0 loaded")
