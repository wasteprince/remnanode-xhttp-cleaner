# RemnaNode XHTTP Cleaner

Безопасная очистка старых TCP-сокетов и связанных с ними XHTTP-буферов процесса `rw-core` в RemnaNode.

**Версия 2.2.0 — by Bankaev.**

Программа не изменяет Config Profile Xray, Docker Compose, лимиты памяти или параметры ядра и не перезапускает контейнер RemnaNode.

## Что очищает программа

Очиститель работает с TCP-сокетами процесса `rw-core` в состояниях:

- `ESTABLISHED`;
- `CLOSE-WAIT`.

Обрабатываются два типа соединений:

- старые исходящие TCP-соединения Xray;
- старые TCP-соединения на listening-портах inbound’ов с транспортом `xhttp` или `splithttp`.

Закрытие неактивного XHTTP-соединения освобождает его сетевые буферы и позволяет Xray завершить связанные обработчики и передать связанные объекты сборщику мусора Go. Программа не подключается к Go heap напрямую и не может удалить внутреннюю XHTTP-очередь, у которой уже нет доступного TCP-сокета.

Показатель `stale_xhttp_buffers` и строка «Старые XHTTP буф.» в панели означают количество найденных старых XHTTP TCP-соединений, а не число байт в памяти.

## Условия закрытия

Сокет становится кандидатом только при одновременном выполнении всех условий:

1. Сокет принадлежит текущему процессу `rw-core` или `xray`: его inode присутствует в `/proc/<pid>/fd`.
2. Сокет находится в состоянии `ESTABLISHED` либо `CLOSE-WAIT`.
3. С момента последней отправки **и** последнего получения данных прошло не менее `idle_seconds` — по умолчанию 300 секунд.
4. Исходящий loopback-сокет исключён, если включён `exclude_loopback`.
5. Inbound-сокет относится к listener’у, который найден в активном конфиге как `xhttp` или `splithttp`. Остальные inbound-соединения по умолчанию исключены.
6. Непосредственно перед закрытием сокет повторно запрашивается через `NETLINK_SOCK_DIAG` и снова проверяется на активность и принадлежность процессу.
7. Inode и 64-битный kernel socket cookie совпадают с первоначальным снимком.

Для распознанного XHTTP-listener’а loopback разрешён: такая схема используется, когда перед Xray работает локальный reverse proxy.

Закрытие выполняется запросом `SOCK_DESTROY` с исходным kernel cookie. Новый сокет, созданный с того же IP или с теми же портами, имеет другой cookie и не пройдёт финальную проверку.

`TIME-WAIT`, `SYN-SENT`, UDP и Unix-domain sockets программа не закрывает.

## Как обнаруживается XHTTP

Перед сканированием программа получает активный собранный конфиг из контейнера RemnaNode:

```text
cli --dump-config-raw
```

Для совместимости также предусмотрен вызов `cli -D`. Из конфига читаются только TCP-inbound’ы, у которых `streamSettings.network` равен `xhttp` или `splithttp`. Содержимое конфига, способное содержать секретные данные, в лог не выводится.

Статус обнаружения отображается как:

- `ok` — конфиг прочитан, XHTTP-listener’ы определены; список может быть пустым;
- `unavailable` — CLI или конфиг недоступен, очистка XHTTP пропущена;
- `disabled` — параметр `clean_xhttp_buffers` отключён.

Если XHTTP обнаружить не удалось, обычная очистка исходящих сокетов продолжает работать.

## Требования

- Ubuntu с systemd;
- запущенный Docker и контейнер RemnaNode;
- права `root`;
- Python 3;
- `nsenter` из пакета `util-linux`;
- ядро Linux с поддержкой `SOCK_DESTROY`.

`install.sh` проверяет, что система является Ubuntu. Docker установщик не устанавливает и не переустанавливает.

## Установка

### 1. Установите Git

```bash
sudo apt update
sudo apt install -y git
```

### 2. Клонируйте репозиторий

Путь `/opt/node-xhttp` используется панелью управления для тестирования и переустановки:

```bash
sudo mkdir -p /opt/node-xhttp
cd /opt/node-xhttp
sudo git clone https://github.com/wasteprince/remnanode-xhttp-cleaner.git .
```

Точка в конце команды клонирует содержимое репозитория непосредственно в `/opt/node-xhttp`.

### 3. Запустите установщик

```bash
cd /opt/node-xhttp
sudo chmod +x install.sh
sudo ./install.sh
```

Установщик:

- проверит Ubuntu, Docker и исходные файлы;
- установит `python3`, `util-linux` и `ca-certificates`;
- найдёт запущенный контейнер RemnaNode;
- создаст `/etc/remnanode-xhttp-clean.json`, если файла ещё нет;
- установит программу в `/usr/local/sbin/remnanode-xhttp-clean`;
- установит команду управления `/usr/local/bin/xhttp-cleaner`;
- создаст и включит systemd service и timer;
- сразу выполнит первую очистку.

Существующая конфигурация при повторной установке сохраняется.

Если контейнер не называется `remnanode`, установщик автоматически выберет его, когда запущен ровно один контейнер с образом `remnawave/node`. Имя также можно указать явно:

```bash
cd /opt/node-xhttp
sudo env REMNANODE_CONTAINER=my-remnanode ./install.sh
```

## Как работает systemd

Программа не держит отдельный процесс постоянно в памяти. systemd timer запускает короткую `oneshot`-службу очистки:

- через 5 минут после загрузки системы;
- через 5 минут после предыдущего запуска;
- с дополнительной случайной задержкой до 20 секунд.

Во время установки первая очистка запускается немедленно, не дожидаясь таймера.

## Управление

Открыть интерактивную панель:

```bash
xhttp-cleaner
```

Панель показывает загрузку сервера, состояние timer, RSS Xray, число TCP-сокетов, старые outbound/XHTTP-соединения, найденные XHTTP-listener’ы и результат последнего запуска.

Доступны и неинтерактивные команды:

```bash
xhttp-cleaner status
xhttp-cleaner scan
xhttp-cleaner clean
xhttp-cleaner logs
xhttp-cleaner logs --follow
xhttp-cleaner enable
xhttp-cleaner disable
xhttp-cleaner test
xhttp-cleaner reinstall
xhttp-cleaner uninstall
xhttp-cleaner help
```

- `status` показывает панель состояния без изменений;
- `scan` выводит кандидатов без закрытия;
- `clean` выполняет финальную проверку и очистку;
- `enable` включает timer и сразу запускает очистку;
- `disable` выключает timer и останавливает только службу очистителя — RemnaNode не затрагивается;
- `test` запускает тесты из `/opt/node-xhttp`;
- `reinstall` запускает `/opt/node-xhttp/install.sh`;
- `uninstall` требует ввести `УДАЛИТЬ` и удаляет установленную программу.

Если команда запущена не от root, панель попробует перезапустить себя через `sudo`.

Прямое управление systemd:

```bash
sudo systemctl status remnanode-xhttp-clean.timer
sudo systemctl start remnanode-xhttp-clean.service
sudo journalctl -u remnanode-xhttp-clean.service -n 100
```

## Обновление

```bash
cd /opt/node-xhttp
sudo git pull
sudo ./install.sh
```

## Ручной запуск

Команды установленной программы:

```bash
sudo remnanode-xhttp-clean status
sudo remnanode-xhttp-clean scan
sudo remnanode-xhttp-clean clean --dry-run
sudo remnanode-xhttp-clean clean
```

Те же команды можно выполнить из репозитория:

```bash
cd /opt/node-xhttp
sudo python3 remnanode-xhttp-clean.py status
sudo python3 remnanode-xhttp-clean.py scan
sudo python3 remnanode-xhttp-clean.py clean --dry-run
```

`status`, `scan` и `--dry-run` ничего не закрывают.

## Конфигурация

Файл `/etc/remnanode-xhttp-clean.json` создаётся со значениями:

```json
{
  "container": "remnanode",
  "idle_seconds": 300,
  "include_inbound": false,
  "exclude_loopback": true,
  "clean_xhttp_buffers": true
}
```

Параметры:

- `container` — имя Docker-контейнера RemnaNode;
- `idle_seconds` — минимальное время без отправки и получения данных; значение меньше 300 запрещено;
- `clean_xhttp_buffers` — разрешить обработку распознанных XHTTP/splithttp TCP-соединений;
- `include_inbound` — разрешить обработку остальных TCP-inbound’ов на listening-портах Xray;
- `exclude_loopback` — исключить loopback для outbound и обычных inbound-соединений. Для распознанного XHTTP этот параметр намеренно не применяется.

`include_inbound` рекомендуется оставлять равным `false`. Если старый конфигурационный файл не содержит `clean_xhttp_buffers`, используется значение по умолчанию `true`.

После изменения конфигурации перезапускать timer не требуется: файл читается при каждом запуске службы.

## Проверка проекта

```bash
cd /opt/node-xhttp
python3 -m py_compile remnanode-xhttp-clean.py
python3 -m unittest -v tests/test_cleaner.py
bash tests/test_install.sh
```

## Удаление

Через панель:

```bash
xhttp-cleaner uninstall
```

Или напрямую, без интерактивного подтверждения панели:

```bash
sudo remnanode-xhttp-clean uninstall
```

Удаляются:

- systemd service и timer;
- `/etc/remnanode-xhttp-clean.json`;
- `/usr/local/sbin/remnanode-xhttp-clean`;
- `/usr/local/bin/xhttp-cleaner`.

Исходники в `/opt/node-xhttp` сохраняются.

## Ограничения

- Программа видит только существующие TCP-сокеты и связанные с ними буферы. Произвольные объекты внутри Go heap Xray она не удаляет.
- XHTTP через Unix-domain socket не обрабатывается.
- XHTTP-listener должен иметь одиночный числовой TCP-порт, а `listen` — IP-адрес, wildcard либо пустое значение.
- Закрытое длительно простаивающее соединение клиент может создать повторно.
- Между финальным чтением `TCP_INFO` и `SOCK_DESTROY` остаётся очень короткий интервал, в котором старый сокет теоретически может получить пакет. Новый socket object при этом защищён kernel cookie.
- RSS Xray может уменьшиться не сразу: Go runtime способен сохранить освобождённые страницы для последующего использования.

## Технические источники

- [Xray-core: реализация серверных XHTTP-сессий и upload queue](https://github.com/XTLS/Xray-core/blob/main/transport/internet/splithttp/hub.go)
- [`ss(8)`: TCP_INFO `lastsnd` и `lastrcv`](https://man7.org/linux/man-pages/man8/ss.8.html)
- [`sock_diag(7)`: получение сведений о сокетах через netlink](https://man7.org/linux/man-pages/man7/sock_diag.7.html)
- [iproute2: `SOCK_DESTROY` отправляется с `inet_diag_sockid`](https://kernel.googlesource.com/pub/scm/network/iproute2/iproute2/+/refs/heads/main/misc/ss.c)
- [Linux UAPI: `inet_diag_sockid.idiag_cookie`](https://codebrowser.dev/linux/linux/include/uapi/linux/inet_diag.h.html)
