# XHTTP Cleaner for RemnaNode

> Безопасный очиститель старых TCP-сокетов Xray

Скрипт очищает старые исходящие TCP-сокеты процесса `rw-core` внутри RemnaNode. Он не меняет Xray Config Profile, Docker Compose, XHTTP-буферы, memory limits или параметры ядра и не перезапускает контейнер.

## Как определяется старый сокет

Сокет закрывается только если выполнены все условия:

1. Он принадлежит текущему процессу `rw-core`: inode присутствует в `/proc/<host-pid>/fd`.
2. Это исходящее соединение: локальный порт не совпадает с listening-портом Xray.
3. Адрес не loopback — внутренние соединения с Nginx/панелью исключены.
4. С момента последней отправки **и** последнего получения данных прошло не менее 300 секунд.
5. Непосредственно перед закрытием TCP_INFO читается повторно; если сокет снова активен, он пропускается.
6. Inode и 64-битный kernel socket cookie совпадают с исходным снимком.

Закрытие выполняется запросом `SOCK_DESTROY` с исходным kernel cookie. Поэтому новый сокет с тем же IP, теми же портами или даже повторно использованным inode не будет закрыт: cookie нового socket object уже другой, и ядро отклонит старый запрос.

Проверяются состояния `ESTABLISHED` и `CLOSE-WAIT`. Для обоих действует один и тот же минимальный простой 5 минут. `TIME-WAIT` не принадлежит процессу и освобождается самим ядром, а `SYN-SENT` не имеет достоверной отметки последнего получения данных, поэтому эти состояния скрипт намеренно не трогает.

## Установка

### 1. Подготовка сервера

Подключитесь к серверу с RemnaNode и установите Git:

```bash
sudo apt update
sudo apt install -y git
```

Docker и контейнер RemnaNode должны быть установлены и запущены до установки XHTTP Cleaner.

### 2. Создание каталога и клонирование репозитория

```bash
sudo mkdir -p /opt/node-xhttp
cd /opt/node-xhttp
sudo git clone https://github.com/wasteprince/remnanode-xhttp-cleaner.git .
```

Точка в конце `git clone` обязательна: она клонирует содержимое репозитория непосредственно в `/opt/node-xhttp`, без вложенного дополнительного каталога.

### 3. Запуск установщика

```bash
cd /opt/node-xhttp
sudo chmod +x install.sh
sudo ./install.sh
```

`install.sh` автоматически:

- установит `python3`, `util-linux` и CA-сертификаты;
- найдёт запущенный контейнер RemnaNode;
- установит очиститель в `/usr/local/sbin/remnanode-xhttp-clean`;
- создаст команду `/usr/local/bin/xhttp-cleaner`;
- создаст и включит systemd timer;
- сразу выполнит первую очистку;
- настроит дальнейшие проверки каждые пять минут, включая после перезагрузки сервера.

Если контейнер называется не `remnanode` и автоматическое определение невозможно:

```bash
cd /opt/node-xhttp
sudo env REMNANODE_CONTAINER=my-remnanode ./install.sh
```

Docker установщик не заменяет и не переустанавливает: он уже является обязательной зависимостью RemnaNode.

## Управление программой

После установки доступна интерактивная панель **XHTTP Cleaner**:

```bash
xhttp-cleaner
```

Из неё можно смотреть состояние и логи, запускать scan/очистку и тесты, включать или выключать timer, переустанавливать и удалять установленную службу. Те же действия доступны без меню:

```bash
xhttp-cleaner status
xhttp-cleaner scan
xhttp-cleaner clean
xhttp-cleaner logs --follow
xhttp-cleaner enable
xhttp-cleaner disable
xhttp-cleaner test
xhttp-cleaner reinstall
```

Проверить systemd timer напрямую:

```bash
sudo systemctl status remnanode-xhttp-clean.timer
sudo journalctl -u remnanode-xhttp-clean.service -n 100
```

## Обновление

```bash
cd /opt/node-xhttp
sudo git pull
sudo ./install.sh
```

Существующая конфигурация `/etc/remnanode-xhttp-clean.json` при повторной установке сохраняется.

## Ручной запуск без systemd

Сначала выполните безопасное сканирование:

```bash
chmod +x remnanode-xhttp-clean.py
sudo ./remnanode-xhttp-clean.py status
sudo ./remnanode-xhttp-clean.py scan
```

Пробный запуск команды очистки:

```bash
sudo ./remnanode-xhttp-clean.py clean --dry-run
```

Закрыть прошедшие повторную проверку сокеты:

```bash
sudo ./remnanode-xhttp-clean.py clean
```

## Конфигурация

После установки используется `/etc/remnanode-xhttp-clean.json`:

```json
{
  "container": "remnanode",
  "idle_seconds": 300,
  "include_inbound": false,
  "exclude_loopback": true
}
```

`idle_seconds` программно запрещено устанавливать ниже 300. Увеличить значение можно. Включать `include_inbound` обычно не следует: тогда очистка сможет закрывать и клиентские XHTTP-соединения на listening-портах.

## Почему не используется `ss -K` по IP

Команда вида `ss -K dst <IP>` может закрыть несколько соединений, включая новое. Даже полный адрес и порт оставляют короткий race между поиском и закрытием при повторном использовании TCP tuple.

Скрипт обращается к `NETLINK_SOCK_DIAG` напрямую. Сначала он получает `inet_diag_sockid` с kernel cookie, затем повторно запрашивает этот же объект и передаёт неизменённый cookie в `SOCK_DESTROY`. Kernel cookie относится к конкретному socket object, а не к IP-адресу.

После `SOCK_DESTROY` буферы сокета в ядре освобождаются, а обслуживающие его объекты Xray становятся доступными для сборщика мусора. RSS процесса может уменьшиться не мгновенно: Go runtime способен некоторое время сохранять уже полученные страницы памяти для повторного использования.

## Требования и ограничения

- Ubuntu/Debian с Linux, Docker, Python 3, `nsenter` из `util-linux` и systemd.
- Нужен root и поддержка `SOCK_DESTROY` ядром Linux.
- Длительно простаивающие push-соединения также считаются неактивными. При пороге 5 минут они будут закрыты и приложение может создать их заново.
- Между финальным чтением TCP_INFO и `SOCK_DESTROY` остаётся микроскопический интервал, в котором старый сокет теоретически может получить пакет. Новый socket object при этом защищён cookie и не будет затронут.

## Проверка

```bash
python3 -m unittest -v tests/test_cleaner.py
python3 -m py_compile remnanode-xhttp-clean.py
bash tests/test_install.sh
```

## Удаление

Через панель управления:

```bash
xhttp-cleaner
```

И выберите пункт удаления. Либо выполните напрямую:

```bash
sudo remnanode-xhttp-clean uninstall
```

Исходный репозиторий в `/opt/node-xhttp` при этом сохраняется, чтобы программу можно было установить повторно.

## Технические источники

- [`ss(8)`: TCP_INFO `lastsnd` и `lastrcv`](https://man7.org/linux/man-pages/man8/ss.8.html)
- [`sock_diag(7)`: получение сведений о сокетах через netlink](https://man7.org/linux/man-pages/man7/sock_diag.7.html)
- [iproute2: `SOCK_DESTROY` отправляется с `inet_diag_sockid`](https://kernel.googlesource.com/pub/scm/network/iproute2/iproute2/+/refs/heads/main/misc/ss.c)
- [Linux UAPI: `inet_diag_sockid.idiag_cookie`](https://codebrowser.dev/linux/linux/include/uapi/linux/inet_diag.h.html)
