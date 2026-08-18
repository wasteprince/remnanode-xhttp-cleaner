> [!CAUTION]
> **Проект оптимизирует только серверный Xray внутри RemnaNode.** Он не устанавливается на клиентские устройства и не может исправить их настройки или расход памяти. После изменения параметров хоста обновите клиентские подписки/профили.

> [!WARNING]
> **Скрипт предназначен только для Xray 26.6.x и более новых версий.** Полный цикл patch/tests/race/build подтверждён для `v26.6.27`, `v26.7.11` и `v26.7.28`. Старые ядра, включая `v26.3.27`, имеют другую внутреннюю реализацию XHTTP `uploadQueue` и не поддерживаются. Для каждой новой версии всё равно выполняется compatibility gate: если структуры Xray изменились, установка безопасно остановится без применения несовместимого патча.

<div align="center">

# 🧬 RemnaNode XHTTP Cleaner

### Безопасный memory fork для XHTTP, Hysteria, TCP и gRPC

[![Version](https://img.shields.io/badge/version-6.0.0-f5c542?style=for-the-badge)](https://github.com/wasteprince/remnanode-xhttp-cleaner)
[![Ubuntu](https://img.shields.io/badge/Ubuntu-supported-E95420?style=for-the-badge&logo=ubuntu&logoColor=white)](#-требования)
[![Xray](https://img.shields.io/badge/Xray-version--matched-1686F0?style=for-the-badge)](#-безопасное-обновление-ядра)
[![License](https://img.shields.io/github/license/wasteprince/remnanode-xhttp-cleaner?style=for-the-badge&color=22c55e)](LICENSE)

**v6.0.0 · by Bankaev**

[Установка](#-установка) · [Архитектура](#-что-изменяет-v600) · [Рекомендации](#-рекомендации-для-настроек-хоста-remnawave) · [Управление](#-управление) · [Откат](#-откат)

</div>

---

> [!IMPORTANT]
> Установщик собирает форк из **точного upstream-тега текущего Xray**, заменяет бинарник внутри уже существующего контейнера и один раз перезапускает этот же контейнер. Docker Compose, env, mounts, ports, networks и restart policy не переписываются. Перезапуск прерывает текущие соединения, поэтому первую установку лучше выполнять в окно обслуживания.

## Зачем появилась v6

V4 разработана после длительного теста RemnaNode под смешанной нагрузкой XHTTP и межсерверного TCP-моста. При примерно 18 тысячах соединений Xray занимал 2,4–2,9 ГиБ RSS, хотя TCP-память cgroup составляла около 50 МиБ: основной объём находился в Go heap/runtime.

Один Hysteria listener обслуживает QUIC через общий UDP-сокет, но каждая логическая UDP-сессия создаёт dispatcher, outbound-сокет, очередь datagram и transport pipes. При `udpIdleTimeout=600` тысячи коротких сессий могут оставаться достижимыми десять минут: kernel socket memory остаётся небольшой, а основной объём находится в Go heap Xray.

V6 сохраняет заданный в конфигурации timeout, добавляет 30 секунд защитного окна и ещё раз проверяет активность под блокировками непосредственно перед закрытием. После безопасного закрытия очередь datagram дренируется, чтобы Go сразу потерял ссылки на старые UDP payload. GC не запускается на каждую сессию: события объединяются в пакеты и обрабатываются с задержкой и cooldown.

> [!NOTE]
> **Оптимизация TCP и gRPC в первую очередь предназначена для серверных outbound-соединений, используемых как мост между двумя серверами.** Долгоживущий межсерверный поток может временно не передавать данные, поэтому Cleaner защищает такие `ESTABLISHED`-соединения от внешнего закрытия и оптимизирует их память через общую pipe policy и memory optimizer внутри Xray.

## ✨ Что изменяет v6.0.0

| Механизм | Что происходит | Защита протоколов |
|---|---|---|
| XHTTP session reaper | Один reaper на listener проверяет полезную upload/download-активность раз в 5 минут | Сессия закрывается после 300 секунд idle и дополнительных 30 секунд grace; `CompareAndDelete` защищает повторно использованный ID |
| Hysteria UDP guard | Использует фактический `udpIdleTimeout` каждого listener и добавляет 30 секунд grace | Последняя проверка активности, `closed`, удаление и очистка очереди согласованы блокировками; активный QUIC не прерывается |
| UDP memory reclaim | После закрытия дренирует только закрытую datagram-очередь; после 256 сессий планирует общий Go reclaim | Перед GC выдерживается 30 секунд, действует адаптивный cooldown; GC на каждое соединение запрещён |
| HTTP keep-alive | `IdleTimeout=5m` освобождает соединение, которое ждёт следующий HTTP request | Активный handler/stream этим timeout не прерывается |
| Общая pipe policy | Стандартный amd64 budget очереди уменьшается с 512 до 128 КиБ на направление | Формат протокола не меняется; явно заданный пользователем `bufferSize` имеет приоритет |
| Config-aware optimizer | Читает уже объединённый Xray protobuf: policy для TCP/WebSocket, XHTTP, gRPC и Hysteria/QUIC; подбирает период reclaim | Таймауты соединений не переписываются; достижимые объекты активных потоков GC удалить не может |
| Cgroup-aware limit | Уважает `GOMEMLIMIT`; иначе ставит мягкий лимит Go до 70% доступного cgroup/host ceiling | Это soft limit, а не OOM-kill и не Docker memory limit |
| Внешняя очистка | По умолчанию рассматривает только `CLOSE_WAIT`, неактивный ≥5 минут, затем ждёт и повторно проверяет ещё 30 секунд | Любой `ESTABLISHED` outbound, включая долгий TCP-мост, защищён по умолчанию |
| Защита от reuse | Перед `SOCK_DESTROY` повторно сверяются inode, tuple и 64-битный kernel cookie | Новый сокет с тем же IP/портами не совпадёт с cookie старого |
| Version gate | Структурные anchors, upstream Go tests, race tests, static build и проверка активного RemnaNode config | При несовместимости новое штатное ядро остаётся нетронутым |
| Rollback | Сохраняются stock binary, checksum, container ID и Docker inspect | Ошибка старта автоматически возвращает оригинальный бинарник |

### Как освобождается память

После завершения сессии Go не обязан сразу вернуть страницы ОС. Optimizer вызывает `runtime.GC()` и `debug.FreeOSMemory()` только при runtime footprint не ниже `max(256 MiB, memory ceiling / 16)`. Базовый период — 5 минут; фактический период равен `min(5m, max(2m, shortestTimeout/2 + 30s))`. Для Hysteria ранний запуск возможен только после пакета из 256 закрытых UDP-сессий, 30 секунд ожидания и того же cooldown.

В расчёт входят эффективный `policy.connectionIdle`, `grpc.idleTimeout`, XHTTP `hMaxReusableSecs`/stream-up и внутренний reaper, Hysteria `udpIdleTimeout`, а также QUIC `maxIdleTimeout`. При нескольких listener используется самый короткий срок. Это меняет только расписание обслуживания памяти — не время жизни соединений и не параметры протоколов.

> [!NOTE]
> RSS не обязан упасть до условных 100–200 МиБ. Память активных сессий, таблиц маршрутизации, TLS/Reality, статистики и фрагментация heap остаются. Оптимизатор возвращает только действительно свободную память.

## ⚙️ Оптимизация CPU

Служебная нагрузка ограничена одним reaper на listener и единым GC-циклом с batch/cooldown. Cleaner намеренно не меняет gRPC BDP и HTTP/2 windows, Linux TCP autotuning/BBR, XHTTP framing и padding, Hysteria QUIC windows/congestion и timeout живого TCP/gRPC-потока: агрессивное уменьшение этих параметров может снизить throughput. В панели `100% CPU` означает один vCore.

## 🔧 Рекомендации для настроек хоста Remnawave

Эти параметры необходимо указывать **в настройках хоста Remnawave**. Они не относятся к JSON-конфигурации Cleaner, Docker Compose, Linux `sysctl` или ручной настройке клиентского приложения.

### XHTTP-параметры хоста

```json
{
  "xmux": {
    "cMaxReuseTimes": "200-300",
    "maxConnections": 1,
    "hKeepAlivePeriod": 60,
    "hMaxRequestTimes": "200-300",
    "hMaxReusableSecs": "600-900"
  },
  "xPaddingBytes": "100-500",
  "scMaxEachPostBytes": "393216-786432"
}
```

Эти значения ограничивают чрезмерно долгое повторное использование XHTTP-соединений и запросов, сохраняя контролируемый жизненный цикл mux-сессии. Диапазоны оставлены строками, как их ожидает конфигурация XHTTP.

### SockOpt-параметры хоста

```json
{
  "tcpcongestion": "bbr",
  "domainStrategy": "AsIs",
  "tcpUserTimeout": 10000,
  "tcpKeepAliveIdle": 300,
  "tcpKeepAliveInterval": 60
}
```

SockOpt задаёт предсказуемое обнаружение неработающих TCP-путей и keep-alive без агрессивного закрытия активного трафика. `tcpUserTimeout` указывается в миллисекундах, а `tcpKeepAliveIdle` и `tcpKeepAliveInterval` — в секундах.

Не смешивайте оба блока: XHTTP-параметры должны находиться в XHTTP-настройках хоста, а SockOpt — в соответствующем поле SockOpt этого же хоста.

### Рекомендуемый Hysteria2-профиль

Ниже профиль, с которым рассчитана и проверяется v6. Замените пути к сертификату и ключу; список `clients` обычно заполняет Remnawave.

```json
{
  "tag": "HYSTERIA2",
  "port": 443,
  "protocol": "hysteria",
  "settings": {
    "clients": [],
    "version": 2
  },
  "sniffing": {
    "enabled": true,
    "destOverride": ["http", "tls", "quic"]
  },
  "streamSettings": {
    "network": "hysteria",
    "security": "tls",
    "finalmask": {
      "quicParams": {
        "maxIdleTimeout": 20,
        "keepAlivePeriod": 5,
        "maxStreamReceiveWindow": 2097152,
        "disablePathMTUDiscovery": false,
        "initStreamReceiveWindow": 1048576,
        "maxConnectionReceiveWindow": 8388608,
        "initConnectionReceiveWindow": 4194304
      }
    },
    "tlsSettings": {
      "alpn": ["h3"],
      "certificates": [
        {
          "keyFile": "ваш ключ",
          "certificateFile": "ваш сертификат"
        }
      ]
    },
    "hysteriaSettings": {
      "version": 2,
      "masquerade": {"type": "404"},
      "udpIdleTimeout": 600
    }
  }
}
```

`udpIdleTimeout=600` сохраняется: закрытие возможно только после 600 секунд без активности и 30 секунд защитного окна. `maxIdleTimeout=20` относится к QUIC, а размеры окон ограничивают объём одной связи без агрессивного урезания throughput.

> [!WARNING]
> Другие корректные конфигурации поддерживаются механизмом чтения фактических timeout, но сочетания иных timeout, QUIC windows и keep-alive не проходят тот же нагрузочный профиль. Используйте нестандартную конфигурацию на свой риск: проект и автор не несут ответственности за обрывы, деградацию производительности или расход памяти, вызванные такими настройками. Сохраняйте backup и сначала проверяйте изменения на отдельном узле.

## 🚀 Установка

Docker, запущенный контейнер RemnaNode и Xray версии **26.6.x или новее** должны существовать заранее.

### Обновление RemnaNode перед установкой

Если в контейнере используется Xray старее 26.6, сначала обновите RemnaNode:

```bash
cd /opt/remnanode && docker compose pull && docker compose down && docker compose up -d && docker compose logs -f
```

`down/up` пересоздаёт контейнер и прерывает подключения. После запуска проверьте фактическую версию Xray: если она всё ещё ниже 26.6, Cleaner устанавливать нельзя.

### Установка Cleaner

```bash
sudo apt update
sudo apt install -y git

sudo mkdir -p /opt/node-xhttp
cd /opt/node-xhttp
sudo git clone https://github.com/wasteprince/remnanode-xhttp-cleaner.git .

sudo chmod +x install.sh
sudo ./install.sh
```

Если контейнер называется не `remnanode`:

```bash
cd /opt/node-xhttp
sudo env REMNANODE_CONTAINER=my-remnanode ./install.sh
```

Первая сборка загружает нужный Go builder и зависимости, запускает проверки, устанавливает бинарник, включает systemd timer и выполняет первый запуск. Готовый artifact и Go module cache сохраняются для следующих обновлений.

Открыть панель:

```bash
xhttp-cleaner
```

## 🎛️ Управление

| Команда | Действие |
|---|---|
| `xhttp-cleaner` | Интерактивная панель by Bankaev |
| `xhttp-cleaner status` | RAM, CPU Xray, сокеты, transports, marker и статистика reclaim |
| `xhttp-cleaner scan` | Показать только разрешённых конфигурацией кандидатов без изменений |
| `xhttp-cleaner clean` | Повторно проверить и закрыть безопасных кандидатов |
| `xhttp-cleaner logs [--follow]` | Показать журнал или следить за ним |
| `xhttp-cleaner enable` | Включить timer и сразу выполнить обслуживание |
| `xhttp-cleaner disable` | Остановить внешний timer; внутренний код действует до rollback/restart |
| `xhttp-cleaner test` | Запустить тесты репозитория |
| `xhttp-cleaner core-update` | Повторить compatibility gate и установку форка |
| `xhttp-cleaner core-rollback` | Восстановить сохранённый оригинальный Xray |
| `xhttp-cleaner reinstall` | Повторно запустить `/opt/node-xhttp/install.sh` |
| `xhttp-cleaner uninstall` | Сначала вернуть stock Xray, затем удалить программу |

Обновить проект:

```bash
cd /opt/node-xhttp
sudo git pull
sudo ./install.sh
```

## 🔄 Безопасное обновление ядра

Timer запускается через пять минут после загрузки и далее раз в пять минут. `ExecStartPre` проверяет marker и версию ядра.

- Для уже установленного `xhttp-cleaner-v6` той же версии сборка не повторяется; после пересоздания контейнера используется artifact нужной версии и архитектуры.
- Для новой версии клонируется точный тег, а patch/tests/race/build gate выполняется заново.
- При изменении structural anchors patcher останавливается **до первой записи**, а неудачная версия не пересобирается таймером каждые пять минут.
- При обновлении с v3 сначала восстанавливается сохранённый stock binary, поэтому старый форк не становится «оригиналом».

Ни один patch не может честно гарантировать совместимость со всеми будущими внутренними изменениями Xray. Здесь гарантия другая: новая версия либо полностью патчится, тестируется и проходит runtime validation, либо остаётся штатной.

## ↩ Откат

```bash
sudo xhttp-cleaner core-rollback
```

Откат сверяет checksum и container ID, возвращает оригинальный файл и перезапускает тот же контейнер. Если контейнер уже пересоздан, перенос старого бинарника намеренно запрещён.

При неуспешном старте диагностический snapshot сохраняется с правами `0600`:

```text
/var/lib/remnanode-xhttp-clean/diagnostics/*-health-failure-*.json
```

Он может содержать IP из Docker logs — проверяйте файл перед публикацией.

## 🧰 Конфигурация внешней страховки

`/etc/remnanode-xhttp-clean.json`:

```json
{
  "container": "remnanode",
  "idle_seconds": 300,
  "include_inbound": false,
  "exclude_loopback": true,
  "clean_xhttp_buffers": false,
  "clean_close_wait": true,
  "clean_established_outbound": false
}
```

Безопасные значения по умолчанию означают:

- `CLOSE_WAIT` можно закрыть только после ≥300 секунд без данных;
- обычные `ESTABLISHED` outbound не закрываются — долгий TCP-мост защищён;
- XHTTP `ESTABLISHED` обслуживает внутренний reaper, поэтому внешний destroy выключен;
- inbound и loopback не затрагиваются.

`idle_seconds` нельзя установить ниже 300. Перед `SOCK_DESTROY` Cleaner ждёт 30 секунд, заново проверяет activity, ownership, inode, tuple и kernel cookie. Включение `clean_established_outbound`, `clean_xhttp_buffers` или `include_inbound` — осознанный fallback-режим: намеренно idle-соединение всё равно может быть оборвано.

### Настройки memory optimizer

Optimizer сначала уважает существующий `GOMEMLIMIT`. Дополнительные env предназначены для опытного администратора и должны задаваться контейнеру штатным способом RemnaNode/Docker:

| Переменная | Значение по умолчанию |
|---|---|
| `XRAY_MEMORY_OPTIMIZER` | включён; `false` отключает |
| `XRAY_MEMORY_OPTIMIZER_INTERVAL` | `5m`, минимум `1m` |
| `XRAY_MEMORY_LIMIT` | автоматически 70% эффективного ceiling |
| `XRAY_MEMORY_OPTIMIZER_MIN_BYTES` | `max(256MiB, ceiling/16)` |
| `XRAY_MEMORY_OPTIMIZER_FORCE` | `false`; `true` запускает reclaim каждый tick |
| `XRAY_MEMORY_OPTIMIZER_STATUS` | `/tmp/xray-memory-optimizer.json` |
| `XRAY_MEMORY_OPTIMIZER_UDP_BATCH` | `256`; допустимо `64..65536` |
| `XRAY_HYSTERIA_UDP_IDLE_CAP` | выключен; опциональный cap `30s..10m`, `off` сохраняет конфигурацию |
| `XHTTP_CLEANER_KEEP_BUILD_CACHE` | пусто; `true` сохраняет Go build cache на host |

Скрипт не редактирует Compose и не добавляет эти env автоматически.

## 📁 Файлы

Команды устанавливаются в `/usr/local/{bin,sbin}`, assets — в `/usr/local/lib/remnanode-xhttp-clean`, конфигурация — в `/etc/remnanode-xhttp-clean.json`, а service/timer — в `/etc/systemd/system`. Backup и diagnostics хранятся в `/var/lib/remnanode-xhttp-clean`, artifacts — в `/var/cache/remnanode-xhttp-clean`.

> [!WARNING]
> Diagnostics могут содержать IP или секретные env. State-файлы создаются `0600`, backup-каталоги — `0700`; проверяйте их перед публикацией.

## 🧪 Проверки

```bash
cd /opt/node-xhttp
python3 -m unittest discover -s tests -v
bash tests/test_install.sh
```

Реальная сборка дополнительно выполняет:

- upstream tests для `splithttp`, `grpc`, `hysteria`, `policy` и `main`;
- race tests XHTTP reaper, Hysteria guard и memory optimizer;
- `gofmt` и статическую `CGO_ENABLED=0` сборку;
- запуск нового binary в test mode на текущем rendered config;
- после рестарта — проверку container ID, Docker settings, процесса и build marker.

## 📋 Требования

- Ubuntu с systemd;
- root-доступ;
- работающие Docker и RemnaNode;
- Xray 26.6.x или новее; более старые реализации XHTTP не поддерживаются;
- архитектура `amd64` или `arm64`;
- доступ к GitHub и Docker Hub при новой сборке;
- свободное место для исходников, modules и builder image.

Установщик добавляет `python3`, `git`, `util-linux` и CA certificates. Docker заранее не устанавливается и не перенастраивается.

## ⚠️ Ограничения

- Установка и откат перезапускают контейнер и обрывают текущие соединения.
- Активный поток никогда не освобождается как «старый», поэтому его рабочая память остаётся.
- Hysteria guard по умолчанию сохраняет `udpIdleTimeout`; его меняет только явно заданный `XRAY_HYSTERIA_UDP_IDLE_CAP`.
- Явный `bufferSize` в policy может переопределить новый default.
- Установка поверх неизвестного стороннего форка не выполняется автоматически.
- `FreeOSMemory` не является обещанием конкретного RSS: результат зависит от live heap и нагрузки.

## 🗑️ Удаление

```bash
xhttp-cleaner uninstall
```

После подтверждения сначала восстанавливается stock Xray. Только после успешного восстановления удаляются service, timer, команда, state и project cache. Git-каталог `/opt/node-xhttp` остаётся.

## 📚 Технические источники

- [Xray transport policy и `bufferSize`](https://xtls.github.io/en/config/policy.html)
- [Xray gRPC transport](https://xtls.github.io/config/transports/grpc.html)
- [Xray-core v26.7.28 XHTTP handler](https://github.com/XTLS/Xray-core/blob/v26.7.28/transport/internet/splithttp/hub.go)
- [Xray-core v26.7.28 Hysteria session manager](https://github.com/XTLS/Xray-core/blob/v26.7.28/transport/internet/hysteria/conn.go)
- [Go GC guide: soft memory limit и RSS model](https://go.dev/doc/gc-guide)
- [Go runtime/debug](https://pkg.go.dev/runtime/debug)
- [Linux cgroup v2 memory controller](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)
- [Linux `sock_diag(7)`](https://man7.org/linux/man-pages/man7/sock_diag.7.html)

---

<div align="center">

Сделано **by Bankaev**

[⬆ Вернуться к началу](#-remnanode-xhttp-cleaner)

</div>
