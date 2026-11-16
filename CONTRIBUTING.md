# Разработка

## Структура

- `src/lianli_lcd/` — discovery, LCD/RGB transport, gRPC, telemetry и Codex exporter.
- `proto/`, `src/coolercontrol/` — protobuf CoolerControl и сгенерированный Python.
- `patches/` — полный патч CoolerControl 4.3.1, включая Rust, UI и тесты.
- `scripts/`, `packaging/` — сборка, установка, удаление и манифест.
- `tests/` — тесты без физического USB; `tests/vendor/tinyuz` — независимый декодер.
- `docs/images/` — примеры рендерера, не снимки пользовательских показаний.

## Python

```bash
/usr/bin/python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
.venv/bin/ruff check src/lianli_lcd tests scripts/install-openai-limits.py
```

Для тестов tinyuz нужен C-компилятор. USB/root и авторизация Codex не нужны.
Не помещайте credentials, proxy-конфигурации и снимки аккаунта в коммиты.

## Rust и UI

```bash
bash scripts/build-compat.sh
```

Скрипт получает `build/coolercontrol-4.3.1` с закреплённым commit и применяет
патч. Редактируйте это дерево, запустите проверки и обновите патч.
Новые исходные файлы добавьте через `git add -N` во вложенном репозитории
перед формированием diff:

```bash
git -C build/coolercontrol-4.3.1 diff --check
git -C build/coolercontrol-4.3.1 diff --binary > patches/coolercontrol-4.3.1-plugin-lcd.patch
```

Не добавляйте в патч скомпилированные assets, `node_modules` или `target`.
Миниатюры создаёт пример `sensor_style_previews`. Патч должен применяться
к чистому исходному commit; изменённое дерево должно проходить
`git apply --reverse --check`.

Тесты главного цикла из `build/coolercontrol-4.3.1/coolercontrold`:

```bash
cargo test --locked --release -p coolercontrold main_loop::tests
```

## Пакеты

```bash
bash scripts/prepare-wheels.sh
```

Wheels собираются системным Python целевой Ubuntu. `build/`, `dist/` и
`artifacts/` исключены из Git. Бинарники для распространения следует
прикладывать к GitHub Releases вместе с соответствующим исходным кодом,
патчем, лицензиями и контрольными суммами.
