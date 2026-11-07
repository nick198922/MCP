**Крок:** p — постійна картка `standing-improvements` (відкритих пунктів було менше трьох), додано і виконано пункт 226.

**Результат:**
• `verify_pm_h11_recheck.py` і `verify_pm_h7_qlike.py` імпортують сусідній скрипт звичайним `import` замість `importlib` за шляхом.
• Два записи з `ALLOWED` у `tests/test_scripts_import_safe.py` прибрано (лишилось 3).
• Числа й вивід скриптів не змінено; `--help` працює з будь-якого cwd.
• `make lint` чисто, `make test`: 7009 passed, 3 skipped.

**Критик:** approve (sonnet, режим C), [рев'ю](../../reviews/2026-11-07-scripts-import-sibling.md); два зауваження без блокування.

**Далі:** `owner`. Нових питань нема. Інші картки або чекають вікон спостережень, або власника.
