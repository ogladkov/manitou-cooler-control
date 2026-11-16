# Manitou CoolerControl

Неофициальный плагин **Lian Li UNI TL LCD / TL LCD Wireless** для
**CoolerControl 4.3.1 на Linux**, с управлением LCD и RGB через штатный интерфейс.
Версия Python-плагина: **0.4.1**. Проверено на Ubuntu 24.04, одном проводном
TL LCD 140 и шести TL LCD Wireless.

Проект состоит из Python-плагина и патча демона/веб-интерфейса CoolerControl.
Совместимая сборка устанавливается отдельно от пакетного демона.
Это независимый проект, не официальный продукт Lian Li или CoolerControl.

## Возможности

- Независимые LCD 400×400: PNG/JPEG, GIF, карусель, яркость и поворот.
- Температуры, RPM, CPU/GPU load, RAM/VRAM used и остаток недельного лимита Codex.
- 15 оформлений; графики с историей 60 точек и обновлением примерно раз в секунду.
- Mono Trace с цветовыми порогами, до 8 правил на экран.
- RGB проводных TL-контроллеров и уже привязанных TL LCD Wireless-групп.
- Симуляция устройств и тестирование без USB-оборудования.

| Mono Reverse | Mono Paper (Dark) |
| --- | --- |
| ![Кольцо с графиком](docs/images/mono_reverse.png) | ![Тёмный график](docs/images/mono_graph.png) |

## Совместимость

| Устройство | USB ID | Поддержка |
| --- | --- | --- |
| UNI TL LCD, проводной | `04fc:7393` | LCD |
| UNI TL LCD Wireless | `1cbe:0006` | LCD |
| TL hub | `0416:7372` | RGB, чтение RPM |
| Wireless RX / TX | `0416:8041` / `0416:8040` | RGB и RPM существующих групп |

Плагин не изменяет PWM, привязку Wireless-групп или прошивку.
Скоростью управляют существующие устройства CoolerControl.
GPU/VRAM из **Lian Li System Metrics** используют NVIDIA `nvidia-smi`;
штатные RPM/load-каналы CoolerControl также доступны как LCD-источники.

Патч рассчитан **только на CoolerControl 4.3.1**, исходный commit
`b255e1e768eba4cec374acb0b463c942ee82d645`. Для другой версии нужна адаптация.
Патч исправляет классификацию LCD-режимов плагина, разделяет файлы кадров
между экранами и добавляет стили, метрики и планировщик обновления.

## Сборка и установка

Бинарники и wheels не хранятся в Git. Соберите их на целевой машине.
Нужны системный Python 3.10+, Rust 1.90+, Node.js 22.12+ и npm.
Сборка проверена с Python 3.12 и Rust 1.90.

Базовые зависимости Ubuntu:

```bash
sudo apt install git python3-venv build-essential pkg-config libdrm-dev libusb-1.0-0 protobuf-compiler
```

После установки Rust и Node.js:

```bash
git clone https://github.com/ogladkov/manitou-cooler-control.git
cd manitou-cooler-control
/usr/bin/python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q
.venv/bin/ruff check src/lianli_lcd tests scripts/install-openai-limits.py
bash scripts/prepare-wheels.sh
bash scripts/build-compat.sh
sudo bash scripts/install.sh --with-compat
sudo systemctl restart coolercontrold
```

`build-compat.sh` получает закреплённые исходники, применяет
[патч](patches/coolercontrol-4.3.1-plugin-lcd.patch), создаёт миниатюры,
проверяет UI/Rust и собирает демон со встроенным веб-интерфейсом.
Без системного `protoc` используется `scripts/protoc` из `.venv`.

Установщик использует wheels из `artifacts/wheels/` офлайн и создаёт
`/opt/cc-lianli-lcd` и systemd override. Пакетный `/usr/bin/coolercontrold`
не перезаписывается. Настройки и кривые сохраняются. Перезапуск кратковременно
прерывает управление охлаждением.

Обновите страницу CoolerControl. Откройте **Lian Li UNI TL LCD** или
**Lian Li UNI TL LCD Wireless** → **LCD** → режим, источник и стиль → **Сохранить**.

Для последующих обновлений после пересборки:

```bash
# Только Python-плагин:
sudo bash scripts/install.sh
# Только совместимый демон и стили:
sudo bash scripts/update-styles.sh
sudo systemctl restart coolercontrold
```

## Показатели и стили

В режиме **Показатель / Sensor** доступны температуры, RPM и проценты.
CPU load, RAM used, NVIDIA GPU load и VRAM used находятся в **Lian Li System Metrics**.
RAM использует MemAvailable, VRAM — долю занятой видеопамяти.
RPM обнаруженных вентиляторов находятся у RGB-устройств.

Сбор показаний и целевое обновление LCD — примерно раз в секунду при
`poll_rate = 1.0`. Если предыдущая передача ещё идёт, следующий цикл пропускается.
История 60 точек — примерно минута; она очищается при сохранении настройки
и перезапуске. Шкалы: 0–100 для °C/%, 0–4000 для RPM.

| Стиль | Вид |
| --- | --- |
| CoolerControl | Исходное оформление |
| Digital, Ring, Dial, Trend | Цифры, кольцо, циферблат, график |
| Mono Minimal, Mono Dial, Mono Segment | Монохромная типографика, шкала, сегменты |
| Mono Paper | Светлые показания и график на чёрном фоне |
| Alien Orbit, Alien Relic, Alien Signal | Монохромные геометрические оформления |
| Mono Trace | Только график; опциональные цветовые пороги |
| Mono Reserve | Исчезающее кольцо без цифр |
| Mono Reverse | Кольцо и график одного источника внутри |

**Mono Trace:** добавьте правило «При значении ≥» и цвет в настройках LCD.
Наибольший достигнутый порог окрашивает весь график; без правил линия белая.
Правила сохраняются отдельно для каждого экрана. При смене источника
проверьте единицы порогов. При отсутствии данных график чёрный.

**Mono Reserve / Mono Reverse:** длина дуги соответствует текущему значению.
Для остатка лимита 100% — полное кольцо, 0% — отсутствие кольца.
В Mono Reverse история остаётся видна при 0%; нет данных — `--`.

В 0.4.1 проводной TL LCD повторяет последний кадр каждую секунду даже при
неизменном показателе. Длинные кадры GIF повторяются без ускорения анимации;
Wireless-изображения не требуют такого повтора.

## Недельные лимиты Codex (опционально)

**Codex weekly left** — остаток недельного лимита Codex по подписке ChatGPT,
а не лимиты обычного чата или API billing. Нужны Codex CLI и вход у
desktop-пользователя в стандартном `~/.codex`.

Если нужен прокси, сохраните его настройки из рабочего терминала **без sudo**:

```bash
python3 scripts/install-openai-limits.py --prepare
```

После сборки установите сборщик:

```bash
sudo bash scripts/install.sh --with-compat --with-openai-limits
sudo systemctl restart coolercontrold
```

Выберите **Показатель → Lian Li System Metrics → Codex weekly left** и стиль.
Служба читает `account/rateLimits/read` раз в минуту от пользователя, вызвавшего
`sudo`. Запросов к модели нет. USB-плагин читает очищенный снимок 0600 в
`/var/lib/cc-lianli-lcd-limits/usage.json`; авторизация не копируется.
Снимок старше 180 секунд, ошибка запроса или наступивший сброс означают
отсутствие данных. Для новых видов лимитов перезапустите CoolerControl.

Proxy-конфигурация находится в игнорируемом `build/`; установщик переносит
её в закрытый `/opt/cc-lianli-lcd/openai-limits.env`. После смены прокси
повторите `--prepare` и установку сборщика.

## RGB

RGB-канал → **Lighting / Подсветка** → эффект → **Сохранить**.
Доступны Off, Static, Rainbow, Color cycle, Meteor, Wave;
для Wireless также Breathing. У применимых эффектов есть цвет, скорость и направление.
Проводной канал управляет портом TL hub, Wireless — существующей группой.
Применение эффекта отключает синхронизацию RGB с материнской платой.
Не запускайте одновременно другую программу, управляющую теми же USB LCD/RGB.

## Разработка и диагностика

```bash
.venv/bin/cc-lianli-lcd list
.venv/bin/cc-lianli-lcd serve --simulate --socket /tmp/lianli-lcd-dev/service.sock --image-root "$PWD"
```

`list` читает только sysfs. Симуляция предоставляет 7 LCD, RGB-устройства
и системные метрики через тот же gRPC API, без USB-доступа.
Для установленной версии:

```bash
systemctl status coolercontrold cc-plugin-lianli-lcd
journalctl -u coolercontrold -u cc-plugin-lianli-lcd -n 100 --no-pager
systemctl status cc-lianli-openai-limits.timer
```

Структура и проверки: [CONTRIBUTING.md](CONTRIBUTING.md),
[docs/verification.md](docs/verification.md).

## Ограничения и удаление

- Нет MP4/H.264, обновления прошивки и записи boot image.
- GIF: до 15 кадров/с; пределы — 20 MiB входа, 600 кадров,
  32 MiB декодированных JPEG и 101888 байт на JPEG.
- ID привязаны к USB-топологии; смена порта создаёт новый ID.
  После изменения состава оборудования нужен перезапуск.
- Изображения читаются из `/etc/coolercontrol` и `/var/lib/coolercontrol`.
  Для другого каталога добавьте `--image-root` в манифест.
- Тесты не гарантируют совместимость с любой прошивкой.

Возврат к пакетному CoolerControl:

```bash
sudo bash scripts/uninstall.sh
sudo systemctl restart coolercontrold
```

Удаляются регистрация и override; файлы в `/opt`, настройки и кривые сохраняются.

## Лицензия

[GPL-3.0-or-later](LICENSE). Заимствованные протоколы и protobuf:
[THIRD_PARTY.md](THIRD_PARTY.md). При распространении совместимого бинарника
предоставляйте соответствующие исходники CoolerControl, патч и лицензии.
