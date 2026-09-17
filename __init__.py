from functools import partial
from urllib.parse import quote_plus

from PyQt6.QtWidgets import (
    QCheckBox, QLabel, QLineEdit, QVBoxLayout, QWidget,
)

from picard.plugin3.api import OptionsPage, t_


# ─── Поиск треков через публичный эндпоинт Яндекс Музыки ───────────────

def _build_search_url(artist, title):
    """Создаёт URL для поиска трека на Яндекс Музыке."""
    query = quote_plus(f"{artist} {title}")
    return "music.yandex.ru", f"/handlers/search.jsx?text={query}&type=tracks&page=0"


def _normalize(s):
    """Нормализует строку для сравнения."""
    return s.lower().strip() if s else ""


def _match_track(track_data, artist, title):
    """Проверяет, что найденный трек похож на искомый."""
    if not track_data:
        return False
    found_title = _normalize(track_data.get("title", ""))
    if _normalize(title) not in found_title and found_title not in _normalize(title):
        return False
    found_artists = [_normalize(a.get("name", "")) for a in track_data.get("artists", [])]
    target = _normalize(artist)
    return any(target in fa or fa in target for fa in found_artists)


# ─── Обработка ответа ─────────────────────────────────────────────────

def _apply_yandex_tags(api, metadata, track_data, album_data):
    """Применяет данные из Яндекс Музыки к метаданным Picard."""
    if not track_data:
        return

    # Жанр
    genre = (album_data or {}).get("genre") or track_data.get("genre")
    if genre:
        metadata["yandex_genre"] = genre

    # Метка explicit
    if track_data.get("explicit", False):
        metadata["yandex_explicit"] = "true"

    # Текст песни (если есть в ответе)
    lyrics = track_data.get("lyrics")
    if lyrics:
        metadata["lyrics"] = lyrics.get("full_lyrics", lyrics) if isinstance(lyrics, dict) else lyrics


def _handle_search_response(api, album, metadata, task_id, artist, title, response, reply, error):
    """Callback для обработки ответа API Яндекс Музыки."""
    try:
        if error:
            api.logger.error(f"Yandex Music: ошибка запроса — {error}")
            return

        data = response.json() if response else None
        if not data:
            return

        tracks = data.get("tracks", {}).get("results", [])
        if not tracks:
            api.logger.debug(f"Yandex Music: ничего не найдено для «{artist} — {title}»")
            return

        # Берём первый совпадающий трек
        for track_data in tracks:
            if _match_track(track_data, artist, title):
                album_data = (track_data.get("albums") or [{}])[0]
                _apply_yandex_tags(api, metadata, track_data, album_data)
                api.logger.info(
                    f"Yandex Music: теги добавлены для «{artist} — {title}»"
                )
                break
        else:
            api.logger.debug(f"Yandex Music: нет точного совпадения для «{artist} — {title}»")

    finally:
        api.complete_album_task(album, task_id)


# ─── Metadata процессоры ──────────────────────────────────────────────

def process_track(api, track, metadata, track_node, release_node=None):
    """Трековый процессор: ищет трек на Яндекс Музыке и дополняет теги."""
    enabled = api.plugin_config.get("enabled", True)
    if not enabled:
        return

    title = metadata.get("title", "")
    artist = metadata.get("artist", "")
    if not title or not artist:
        return

    # Уникальный ID задачи
    task_id = f"ya_search_{title}_{artist}"
    api.add_album_task(
        track.album,
        task_id,
        f"Searching Yandex Music: {artist} — {title}",
        timeout=15.0,
    )

    host, path = _build_search_url(artist, title)

    def response_handler(response, reply, error):
        _handle_search_response(
            api, track.album, metadata, task_id,
            artist, title, response, reply, error,
        )

    request = api.web_service.get(
        host, path,
        response_handler,
        priority=True,
        important=False,
    )
    api.set_album_task_request(track.album, task_id, request)


# ─── Страница настроек ────────────────────────────────────────────────

class YandexMusicOptionsPage(OptionsPage):
    NAME = "yandex_music"
    TITLE = t_("Yandex Music")
    PARENT = "plugins"

    def __init__(self):
        super().__init__()
        self.layout_main = QVBoxLayout(self)

        # Чекбокс включения
        self.enabled_cb = QCheckBox("Fetch metadata from Yandex Music")
        self.layout_main.addWidget(self.enabled_cb)

        # Поле для токена (опционально)
        self.token_label = QLabel("Yandex Music token (optional, for lyrics & full access):")
        self.layout_main.addWidget(self.token_label)

        self.token_input = QLineEdit()
        self.token_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.token_input.setPlaceholderText("Paste your Yandex Music OAuth token here")
        self.layout_main.addWidget(self.token_input)

        # Чекбоксы для отдельных тегов
        self.fetch_lyrics_cb = QCheckBox("Fetch lyrics")
        self.fetch_genre_cb = QCheckBox("Fetch genre")
        self.fetch_explicit_cb = QCheckBox("Fetch explicit flag")
        self.layout_main.addWidget(self.fetch_lyrics_cb)
        self.layout_main.addWidget(self.fetch_genre_cb)
        self.layout_main.addWidget(self.fetch_explicit_cb)

        self.layout_main.addStretch()

    def load(self):
        """Загружает настройки в UI."""
        self.enabled_cb.setChecked(self.api.plugin_config.get("enabled", True))
        self.token_input.setText(self.api.plugin_config.get("token", ""))
        self.fetch_lyrics_cb.setChecked(self.api.plugin_config.get("fetch_lyrics", True))
        self.fetch_genre_cb.setChecked(self.api.plugin_config.get("fetch_genre", True))
        self.fetch_explicit_cb.setChecked(self.api.plugin_config.get("fetch_explicit", True))

    def save(self):
        """Сохраняет настройки из UI."""
        self.api.plugin_config["enabled"] = self.enabled_cb.isChecked()
        self.api.plugin_config["token"] = self.token_input.text()
        self.api.plugin_config["fetch_lyrics"] = self.fetch_lyrics_cb.isChecked()
        self.api.plugin_config["fetch_genre"] = self.fetch_genre_cb.isChecked()
        self.api.plugin_config["fetch_explicit"] = self.fetch_explicit_cb.isChecked()


# ─── Точка входа ──────────────────────────────────────────────────────

def enable(api):
    """Регистрирует все компоненты плагина."""
    # Регистрируем опции с дефолтами
    api.plugin_config.register_option("enabled", True)
    api.plugin_config.register_option("token", "")
    api.plugin_config.register_option("fetch_lyrics", True)
    api.plugin_config.register_option("fetch_genre", True)
    api.plugin_config.register_option("fetch_explicit", True)

    # Регистрируем процессор треков (низкий приоритет — после основных плагинов)
    api.register_track_metadata_processor(process_track, priority=-50)

    # Регистрируем страницу настроек
    api.register_options_page(YandexMusicOptionsPage)

    api.logger.info("Yandex Music Metadata plugin loaded")
