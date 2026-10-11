# -*- coding: utf-8 -*-
"""Менеджер провайдеров распознавания (шаги 0.8, 7.1a, 7.1b): выбор, цепочка «Авто» (порог уверенности,
подтверждение справочником), сторонние провайдеры из plugins/ocr_*, объединение результатов и учёт времени,
облако только с согласия, заглушка облака, правило 8/0, качество."""
import importlib.util
import io
import os
import sys

import pytest

from digger.acquire.events import EventBus, render
from digger.core.models import ImageVariant, OcrLine, OcrResult
from digger.core.pipeline import ChipPipeline
from digger.core.utils import imread
from digger.recognition import manager as manager_module
from digger.recognition.api import OcrProvider
from digger.recognition.manager import ProviderInfo, RecognitionManager

HAS_PPOCR = all(importlib.util.find_spec(m) for m in ("onnxruntime", "rapidocr_onnxruntime"))
needs_ppocr = pytest.mark.skipif(not HAS_PPOCR, reason="не установлен rapidocr-onnxruntime")
SAMPLES = {"at24c02.png": "24C02", "stm32.png": "STM32F103", "w25q64_rot.png": "W25Q64", "lm358_180.png": "LM358"}
# шаг 0.11: на 120 вырезках Win7 (отчёт 2026-10-10) и Mac дают одно и то же — 46 строк совпало с ответом
# (38.3 %), в 61 строке ответ содержится (50.8 %: «25Q128FVPG» при ответе «25Q128FV»); пороги — с запасом в 2–3 фото
MIN_MYTEST_PCT = 36.0
MIN_MYTEST_INSIDE_PCT = 48.0


def _provider(pid, text="", available=True, fail=False, original=False, cloud=False, lines=None, conf=90.0):
    class Provider(OcrProvider):
        id = pid
        title = pid.upper()
        kind = "cloud" if cloud else "local"
        wants_original = original
        calls = []

        def is_available(self):
            return available, "" if available else "нет библиотеки"

        def recognize(self, variants, hints=None, progress=None):
            Provider.calls.append([v.name for v in variants])
            if fail:
                raise RuntimeError("упал")
            texts = lines if lines is not None else ([text] if text else [])
            return OcrResult(lines=[OcrLine(t, conf, variants[0].name) for t in texts], best_text=text)

    return Provider


def _manager(ctx, monkeypatch, chain=None, cloud_enabled=False, **providers):
    """Провайдеры-примеры подставляются как сторонние (будто найдены в `plugins/ocr_<id>/`)."""
    real_load = manager_module.load_provider_class
    monkeypatch.setattr(manager_module, "discover_plugins",
                        lambda folder: [ProviderInfo(id=pid, title=pid.upper()) for pid in providers])
    monkeypatch.setattr(manager_module, "load_provider_class",
                        lambda info: providers[info.id] if info.id in providers else real_load(info))
    monkeypatch.setitem(ctx.config, "recognition", {"chain": chain or list(providers), "providers": {},
                                                    "cloud_enabled": cloud_enabled})
    return RecognitionManager({}, ctx)


def _feed(ctx):
    """Лента: тексты событий цепочки распознавания."""
    ctx.bus = EventBus()
    seen = []
    ctx.bus.subscribe(lambda e: seen.append(render(e)) if e.kind == "ocr" else None)
    return seen


def _write_plugin(plugins, folder, manifest, code):
    path = os.path.join(str(plugins), folder)
    os.makedirs(path)
    with io.open(os.path.join(path, "provider.json"), "w", encoding="utf-8") as f:
        f.write(manifest)
    with io.open(os.path.join(path, "provider.py"), "w", encoding="utf-8") as f:
        f.write(code)


PLUGIN_CODE = u'''# -*- coding: utf-8 -*-
from digger.core.models import OcrLine, OcrResult
from digger.recognition.api import OcrProvider


class Provider(OcrProvider):
    id = "mine"
    title = "Свой способ"

    def recognize(self, variants, hints=None, progress=None):
        text = self.settings.get("answer", "NE555")
        return OcrResult(lines=[OcrLine(text, 88.0, variants[0].name)], best_text=text)
'''


VARIANTS = [ImageVariant("gray", None)]


def test_default_role_is_manager(ctx):
    ocr = ctx.modules["ocr"]
    assert isinstance(ocr, RecognitionManager)
    assert ocr.chain == ["ppocr", "tesseract"]


def test_example_provider_is_chosen(ctx, monkeypatch):
    a, b = _provider("exa", "NE555"), _provider("exb", "LM358")
    m = _manager(ctx, monkeypatch, exa=a, exb=b)
    res = m.recognize(VARIANTS)
    assert (res.best_text, res.provider, res.provider_title) == ("NE555", "exa", "EXA")
    assert not b.calls


def test_wants_original_gets_photo(ctx, monkeypatch):
    a = _provider("exa", "NE555", original=True)
    _manager(ctx, monkeypatch, exa=a).recognize(VARIANTS, original="фото")
    assert a.calls == [["original"]]


@pytest.mark.parametrize("first", [dict(available=False), dict(fail=True), dict(text="")])
def test_next_provider_when_first_fails(ctx, monkeypatch, first):
    m = _manager(ctx, monkeypatch, exa=_provider("exa", **first), exb=_provider("exb", "LM358"))
    assert m.is_available()
    res = m.recognize(VARIANTS)
    assert (res.best_text, res.provider) == ("LM358", "exb")


def test_nothing_available(ctx, monkeypatch):
    m = _manager(ctx, monkeypatch, chain=["exa", "nosuch"], exa=_provider("exa", available=False))
    assert not m.is_available()
    assert "exa" in m.error and "nosuch" in m.error
    res = m.recognize(VARIANTS)
    assert res.best_text == "" and [a.status for a in res.attempts] == ["unavailable", "unavailable"]


def test_plugin_provider_found_and_chosen(ctx, monkeypatch, tmp_path):
    """Папка `plugins/ocr_<имя>/` (provider.json + provider.py) в пути с русскими буквами и пробелом."""
    app = tmp_path / "Папка программы"
    _write_plugin(app / "plugins", "ocr_mine", u'{"id": "mine", "title": "Свой способ", "kind": "local"}', PLUGIN_CODE)
    _write_plugin(app / "plugins", "ocr_broken", u'{"id": "broken"}', u"raise RuntimeError('сломан')\n")
    _write_plugin(app / "plugins", "ocr_badjson", u"{не json", PLUGIN_CODE)
    _write_plugin(app / "plugins", "ocr_other", u'{"id": "other", "title": "Не в цепочке"}', u"raise SystemExit\n")
    monkeypatch.setattr(ctx, "app_dir", str(app))
    monkeypatch.setitem(ctx.config, "recognition", {"chain": ["broken", "badjson", "mine", "tesseract"],
                                                    "providers": {"mine": {"answer": "LM358"}}})
    m = RecognitionManager({}, ctx)
    by = {i.id: i for i in m.catalog()}
    assert {"mine", "broken", "badjson", "other", "ppocr", "tesseract"} <= set(by)
    assert (by["mine"].title, by["mine"].builtin, by["ppocr"].builtin) == (u"Свой способ", False, True)
    assert by["other"].title == u"Не в цепочке" and not by["other"].error     # код вне цепочки не выполняется
    assert "сломан" in m.problems["broken"] and "provider.json" in m.problems["badjson"]
    res = m.recognize(VARIANTS)
    assert (res.best_text, res.provider, res.provider_title) == ("LM358", "mine", u"Свой способ")
    assert [(a.provider, a.status) for a in res.attempts] == [("broken", "unavailable"), ("badjson", "unavailable"),
                                                              ("mine", "ok")]


def test_plugin_cannot_replace_builtin(ctx, monkeypatch, tmp_path):
    _write_plugin(tmp_path / "plugins", "ocr_fake", u'{"id": "tesseract", "title": "Подмена"}', PLUGIN_CODE)
    monkeypatch.setattr(ctx, "app_dir", str(tmp_path))
    m = RecognitionManager({}, ctx)
    assert m.known["tesseract"].builtin and m.known["tesseract"].title == "Tesseract"


def test_results_merged_and_timed(ctx, monkeypatch):
    """Первый прочитал строки, но итога не дал → итог от второго, строки первого — запасными, без повторов."""
    a = _provider("exa", "", lines=["W25Q64", "lm358"])
    b = _provider("exb", "LM358", lines=["LM358", "2231"])
    c = _provider("exc", "NE555")
    m = _manager(ctx, monkeypatch, exa=a, exf=_provider("exf", fail=True), exb=b, exc=c)
    res = m.recognize(VARIANTS)
    assert (res.best_text, res.provider) == ("LM358", "exb")
    assert [(l.text, l.provider) for l in res.lines] == [("LM358", "exb"), ("2231", "exb"), ("W25Q64", "exa")]
    assert [(x.provider, x.status, x.lines) for x in res.attempts] == [("exa", "empty", 2), ("exf", "failed", 0),
                                                                      ("exb", "ok", 2)]
    assert "упал" in res.attempts[1].detail and not c.calls
    assert res.total_seconds >= res.seconds >= 0
    m.recognize(VARIANTS)
    assert m.stats["exb"]["calls"] == 2 and m.stats["exb"]["ok"] == 2
    assert m.stats["exa"]["empty"] == 2 and m.stats["exf"]["failed"] == 2 and "exc" not in m.stats


def test_weak_confidence_goes_to_next(ctx, monkeypatch):
    """Провайдер-неудачник: уверенность ниже порога → следующий; ход виден в ленте."""
    feed = _feed(ctx)
    said = []
    m = _manager(ctx, monkeypatch, exa=_provider("exa", "NE555", conf=42.0), exb=_provider("exb", "LM358"))
    res = m.recognize(VARIANTS, progress=said.append)
    assert (res.best_text, res.provider, res.confidence, res.confirmed) == ("LM358", "exb", 90.0, True)
    assert [(a.provider, a.status, a.confidence) for a in res.attempts] == [("exa", "weak", 42.0), ("exb", "ok", 90.0)]
    assert feed == [u"распознаю: EXA", u"EXA — уверенность 42 %, ниже порога 60 %",
                    u"пробую следующий способ: EXB", u"EXB: прочитано, уверенность 90 %"]
    assert said == feed and m.stats["exa"]["weak"] == 1


def test_unconfirmed_goes_to_next(ctx, monkeypatch):
    """Партномер из прочитанного не подтвердился справочником → следующий провайдер."""
    feed = _feed(ctx)
    m = _manager(ctx, monkeypatch, exa=_provider("exa", "QZX7719KW"), exb=_provider("exb", "LM358"))
    assert m.recognize(VARIANTS).provider == "exa"          # по умолчанию правило выключено (см. «Решения»)
    del feed[:]
    m.require_confirmed = True
    res = m.recognize(VARIANTS)
    assert (res.provider, res.attempts[0].status) == ("exb", "unconfirmed")
    assert u"партномер не подтвердился справочником" in feed[1]
    assert [l.text for l in res.lines] == ["LM358", "QZX7719KW"]


def test_nobody_confident_keeps_first_reading(ctx, monkeypatch):
    """Никто не справился: берётся подтверждённый справочником, иначе первый по цепочке, кто что-то прочитал."""
    feed = _feed(ctx)
    m = _manager(ctx, monkeypatch, exe=_provider("exe"), exa=_provider("exa", "QZX7719KW"),
                 exb=_provider("exb", "PKV0042JJ", conf=99.0))
    m.require_confirmed = True
    res = m.recognize(VARIANTS)
    assert (res.provider, res.confirmed) == ("exa", False)
    assert [a.status for a in res.attempts] == ["empty", "unconfirmed", "unconfirmed"]
    assert feed[-1] == u"уверенного результата нет — взято прочитанное способом EXA"
    m = _manager(ctx, monkeypatch, exa=_provider("exa", "QZX7719KW", conf=50.0), exb=_provider("exb", "LM358", conf=30.0))
    assert m.recognize(VARIANTS).provider == "exb"          # оба слабые, но второй подтверждён справочником


def test_short_confident_noise_does_not_replace_longer_reading(ctx, monkeypatch):
    """Отчёт с Win7 от 2026-10-10: первый способ прочитал четыре строки с уверенностью чуть ниже
    порога, второй — три знака мусора чуть выше порога. Остаётся прочитанное первым: у него прочитано больше."""
    feed = _feed(ctx)
    first = ["BE33E", "CHOH", "QZXB", "5861"]
    m = _manager(ctx, monkeypatch, exa=_provider("exa", "\n".join(first), lines=first, conf=57.0),
                 exb=_provider("exb", "44H", conf=61.0))
    res = m.recognize(VARIANTS)
    assert (res.provider, res.best_text.splitlines()) == ("exa", first)
    assert [(a.provider, a.status) for a in res.attempts] == [("exa", "weak"), ("exb", "ok")]
    assert [l.text for l in res.lines] == first + ["44H"]
    assert feed[-1] == u"EXB прочитал меньше — оставлено прочитанное способом EXA"


def test_later_reading_wins_when_it_reads_more_or_is_confirmed(ctx, monkeypatch):
    m = _manager(ctx, monkeypatch, exa=_provider("exa", "7)", conf=59.0), exb=_provider("exb", "QZX7719KW", conf=61.0))
    assert m.recognize(VARIANTS).provider == "exb"          # второй прочитал больше
    first = ["BE33E", "CHOH", "QZXB", "5861"]
    m = _manager(ctx, monkeypatch, exa=_provider("exa", "\n".join(first), lines=first, conf=57.0),
                 exb=_provider("exb", "LM358", conf=61.0))
    assert m.recognize(VARIANTS).provider == "exb"          # второй подтверждён справочником
    m = _manager(ctx, monkeypatch, exa=_provider("exa", "LM358", conf=40.0), exb=_provider("exb", "QZX7719KW1234"))
    assert m.recognize(VARIANTS).provider == "exa"          # первый слаб, но подтверждён справочником, второй — нет


def test_auto_rules_can_be_switched_off(ctx, monkeypatch):
    m = _manager(ctx, monkeypatch, exa=_provider("exa", "QZX7719KW", conf=20.0), exb=_provider("exb", "LM358"))
    m.min_confidence, m.require_confirmed = 0.0, False
    assert m.recognize(VARIANTS).provider == "exa"


def test_cloud_provider_not_called_without_consent(ctx, monkeypatch):
    cloud = _provider("excloud", "NE555", cloud=True)
    asked = []
    answers = ["no", "once", "session"]

    def ask(pid, title):
        asked.append((pid, title))
        return answers.pop(0)

    # облако выключено в настройках — не вызывается и согласие не спрашивается
    m = _manager(ctx, monkeypatch, excloud=cloud, exb=_provider("exb", "LM358"))
    m.consent.ask = ask
    res = m.recognize(VARIANTS, original="фото")
    assert res.provider == "exb" and res.attempts[0].status == "unavailable" and not asked and not cloud.calls
    # включено, но спросить некого (нет окна) — не вызывается
    feed = _feed(ctx)
    m = _manager(ctx, monkeypatch, cloud_enabled=True, excloud=cloud, exb=_provider("exb", "LM358"))
    assert m.recognize(VARIANTS).attempts[0].status == "no_consent" and not cloud.calls
    assert feed[0] == u"EXCLOUD: фото не отправлено — нет согласия на отправку"
    # ответ «нет» → не вызывается; «это фото» → один раз; «на сессию» → дальше без вопросов
    m.consent.ask = ask
    assert m.recognize(VARIANTS).provider == "exb" and not cloud.calls
    assert m.recognize(VARIANTS).provider == "excloud" and len(cloud.calls) == 1
    assert m.recognize(VARIANTS).provider == "excloud" and len(asked) == 3
    assert m.recognize(VARIANTS).provider == "excloud" and len(asked) == 3 and len(cloud.calls) == 3
    m.consent.revoke()
    answers.append("no")
    assert m.recognize(VARIANTS).provider == "exb" and asked[-1] == ("excloud", "EXCLOUD")


def test_cloud_stub_is_not_configured(ctx, monkeypatch):
    """Заглушка облака: видна в списке способов, в цепочке по умолчанию её нет, согласие для неё не спрашивается."""
    assert "cloud_stub" not in ctx.modules["ocr"].chain
    asked = []
    monkeypatch.setitem(ctx.config, "recognition", {"chain": ["cloud_stub", "exb"], "cloud_enabled": True})
    m = _manager(ctx, monkeypatch, chain=["cloud_stub", "exb"], cloud_enabled=True, exb=_provider("exb", "LM358"))
    m.consent.ask = lambda pid, title: asked.append(pid) or "session"
    info = {i.id: i for i in m.catalog()}["cloud_stub"]
    assert (info.kind, info.builtin) == ("cloud", True) and m.is_available()
    res = m.recognize(VARIANTS)
    assert res.provider == "exb" and not asked
    assert (res.attempts[0].status, res.attempts[0].detail) == ("unavailable", u"не настроено")


def test_removed_cloud_stub_breaks_nothing(ctx, monkeypatch):
    """Папку `providers/cloud_stub/` удалили: программа работает, а её id в цепочке просто пропускается."""
    monkeypatch.setattr(manager_module, "builtin_ids", lambda: ["ppocr", "tesseract"])
    m = _manager(ctx, monkeypatch, chain=["cloud_stub", "exb"], cloud_enabled=True, exb=_provider("exb", "LM358"))
    assert "cloud_stub" not in [i.id for i in m.catalog()]
    assert m.recognize(VARIANTS).provider == "exb" and "cloud_stub" in m.error
    monkeypatch.setitem(ctx.config, "recognition", {})
    assert RecognitionManager({}, ctx).chain == ["ppocr", "tesseract"]


def test_missing_ppocr_falls_back_to_tesseract(ctx, monkeypatch):
    """Нет библиотек PP-OCR (или не загрузились DLL) → читает Tesseract."""
    m = ctx.modules["ocr"]
    monkeypatch.setattr(m.providers["ppocr"], "_error", "PP-OCR не загрузился: ImportError: DLL load failed")
    monkeypatch.setattr(m.providers["tesseract"], "is_available", lambda: (True, ""))
    monkeypatch.setattr(m.providers["tesseract"], "recognize", lambda v, hints=None, progress=None: OcrResult(best_text="LM358"))
    res = m.recognize(VARIANTS, original=None)
    assert res.provider == "tesseract" and "DLL" in m.error


def test_slashed_zero_rule(ctx):
    """Перечёркнутый ноль прочитан как 8: «STM32F183C8T6» → в кандидатах есть STM32F103C8T6."""
    ident = ctx.modules["identifier"]
    cands = ident.identify("W25Q64JV\nSTM32F183C8T6")
    assert "STM32F103C8T6" in [c.part for c in cands]
    cands = ident.identify("PIC16F628A")
    assert cands[0].part == "PIC16F628A"      # настоящая восьмёрка не портится


@needs_ppocr
@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_ppocr_reads_samples(ctx, samples_dir, name):
    """Образцы читаются PP-OCR не хуже v1 (Tesseract): партномер определён."""
    ctx.config["recognition"]["chain"] = ["ppocr"]
    ctx.modules["ocr"] = RecognitionManager({}, ctx)
    r, _v = ChipPipeline(ctx).analyze_image(os.path.join(samples_dir, name))
    assert r.ocr.provider == "ppocr" and r.ocr.seconds > 0
    assert SAMPLES[name] in r.chosen_part


@pytest.mark.needs_tesseract
@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_tesseract_chain_reads_samples(ctx, samples_dir, name):
    ctx.config["recognition"]["chain"] = ["tesseract"]
    ctx.modules["ocr"] = RecognitionManager({}, ctx)
    r, _v = ChipPipeline(ctx).analyze_image(os.path.join(samples_dir, name))
    assert r.ocr.provider == "tesseract"
    assert SAMPLES[name] in r.chosen_part


@needs_ppocr
@pytest.mark.mytest
def test_mytest_ocr_share(ctx, my_test_dir):
    """Доля вырезок my_test/ocr, где строка из имени файла прочитана (критерий шага 0.7)."""
    folder = os.path.join(my_test_dir, "ocr")
    if not os.path.isdir(folder):
        pytest.skip("нет папки my_test/ocr/")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "win7_pack"))
    import ppocr_check as pc
    items = pc.collect(folder)
    if not items:
        pytest.skip("в my_test/ocr/ нет картинок")
    ocr = ctx.modules["ocr"]
    exact = inside = 0
    for path, expected in items:
        res = ocr.recognize([], original=imread(path))
        exact += any(pc.is_match(l.text, expected) for l in res.lines)
        inside += any(expected and expected in pc.norm(l.text) for l in res.lines)
    pct = 100.0 * exact / len(items)
    print("\nmy_test/ocr: %d фото, строка совпала — %.1f %%, строка содержит ответ — %.1f %%"
          % (len(items), pct, 100.0 * inside / len(items)))
    assert pct >= MIN_MYTEST_PCT
    assert 100.0 * inside / len(items) >= MIN_MYTEST_INSIDE_PCT


def test_window_asks_consent_from_background_thread(ctx, monkeypatch):
    """Окно: распознавание идёт в фоне, вопрос об отправке фото задаётся в потоке окна."""
    import threading
    if sys.platform != "win32":
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PyQt5")
    from PyQt5.QtWidgets import QApplication
    from digger.gui.worker import ConsentBridge
    app = QApplication.instance() or QApplication([])
    bridge = ConsentBridge()
    shown = []
    monkeypatch.setattr(bridge, "dialog", lambda title: shown.append((title, threading.current_thread().name)) or "once")
    cloud = _provider("excloud", "NE555", cloud=True)
    m = _manager(ctx, monkeypatch, cloud_enabled=True, excloud=cloud)
    m.consent.ask = bridge.ask
    got = []
    worker = threading.Thread(target=lambda: got.append(m.recognize(VARIANTS)), name="фон")
    worker.start()
    for _ in range(500):
        app.processEvents()
        worker.join(0.01)
        if not worker.is_alive():
            break
    assert got and got[0].provider == "excloud"
    assert shown == [("EXCLOUD", threading.current_thread().name)]
