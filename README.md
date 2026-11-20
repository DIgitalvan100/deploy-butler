# Deploy Butler

Локальная панель деплоя на свой VPS. Без GitHub, без rsync и без пароля sudo: вы сами ставите на сервер маленький bash-помощник, а приложение по SSH только загружает архив, сверяет sha256 и вызывает `sudo` этого помощника.

The same program, in English, is below.

## Установка

Нужен Python 3.11 или новее. На сервере (Linux): bash 4.4+, GNU tar, coreutils (`sha256sum`, `flock`), и `sqlite3`, если бэкапите базы. Для systemd нужен `systemctl`, для pm2 — `pm2` в `PATH` у root.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m deploy_butler
```

Панель слушает только `http://127.0.0.1:8787` и открывает браузер. Чтобы не открывать браузер: `DEPLOY_BUTLER_NO_BROWSER=1`. Другой порт: `DEPLOY_BUTLER_PORT` (по умолчанию 8787). Каталог данных:

- Linux: `~/.local/share/deploy-butler/`
- macOS: `~/Library/Application Support/deploy-butler/`
- Windows: `%APPDATA%\deploy-butler\`

Переопределение: `DEPLOY_BUTLER_HOME`. Там лежат `butler.db`, `known_hosts` и локальные lock-файлы. Содержимое SSH-ключа и пароль sudo не хранятся.

Тесты: `pip install -r requirements-dev.txt` и `pytest`. Для проверки помощника нужен `shellcheck`.

## Как пользоваться

1. «Новый проект». Имя — латиница, цифры, `.`, `_`, `-`. От него называются скрипт и sudoers.
2. Вкладка «Настройки». Папка обновлений, формат (подпапка или tar/zip), SSH, каталог приложения, сервис, бэкапы. Кнопка «Проверить» только смотрит, что папка есть. «Экспорт JSON» / «Импорт JSON» переносят настройки без секретов: путь к ключу — это путь, не сам ключ.
3. Вкладка «Подготовка сервера». Скачайте два файла и выполните шаги от root на VPS. Приложение их не устанавливает.
4. «Обновления». Список сам обновляется. Выберите релиз, сверьте файлы и нажмите «Подтвердить».
5. В конце крупно: «Успешно», «Откачено» или «Ошибка». Журнал шагов идёт в браузер. История хранит лог и умеет откатить состояние до выбранного деплоя.

Обновление — это подпапка (`r35`, `r36-fix`) или архив `.tar.gz` / `.tgz` / `.tar` / `.zip` в папке обновлений. Имя релиза должно совпадать с `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`.

Деплой блокируется, если внутри есть запрещённый файл (по умолчанию `.env`, ключи, базы, `node_modules/`, `.git/`, тесты и сиды), абсолютный путь, `..`, симлинк или жёсткая ссылка в архиве.

## Подготовка сервера

Помощник: `/usr/local/sbin/deploy-butler-<проект>`, владелец `root:root`, режим `0755`.

sudoers: `/etc/sudoers.d/deploy-butler-<проект>`, режим `0440`, одна команда без шаблонов:

```
deploy ALL=(root) NOPASSWD: /usr/local/sbin/deploy-butler-myapp
```

Проверка и удаление тоже написаны на вкладке. Коротко:

```bash
sudo install -o root -g root -m 0755 deploy-butler-myapp /usr/local/sbin/deploy-butler-myapp
sudo install -o root -g root -m 0440 deploy-butler-myapp.sudoers /etc/sudoers.d/deploy-butler-myapp
sudo visudo -cf /etc/sudoers.d/deploy-butler-myapp
sudo visudo -c
sudo -n -u deploy sudo -n /usr/local/sbin/deploy-butler-myapp list-backups
```

Удаление: сначала sudoers, потом скрипт, затем снова `sudo visudo -c`.

Помощник принимает только `hashes`, `backup`, `apply`, `restart`, `status`, `logs`, `rollback`, `list-backups`, `prune`. Параллельный запуск держит `flock`. Имя релиза проверяется. Архив читается только из `/tmp/deploy-butler/<проект>/`. Перед распаковкой отвергаются абсолютные пути, `..`, симлинки и хардлинки. Скрипт не печатает env-файлы и секреты.

На сервере приложение делает лишь три вещи: SFTP в `/tmp/deploy-butler/<проект>/`, `sha256sum` загруженного архива и `sudo -n deploy-butler-<проект> <подкоманда>`. Произвольных команд нет. Пароль sudo не вводится.

Старые бэкапы и релизы сами не удаляются. Кнопка «Очистить, кроме последних N» спрашивает подтверждение и не трогает текущий релиз.

## Пример: Node и systemd

Каталог приложения `/opt/myapp/app`, пользователь `myapp`, сервис `myapp`. Режим «на месте».

`/etc/systemd/system/myapp.service`:

```ini
[Unit]
Description=myapp
After=network.target

[Service]
User=myapp
Group=myapp
WorkingDirectory=/opt/myapp/app
ExecStart=/usr/bin/node /opt/myapp/app/server.js
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now myapp
```

В проекте: `service_type=systemd`, `service_name=myapp`, `app_owner=myapp:myapp`, `app_dir=/opt/myapp/app`. Health URL должен открываться с компьютера, где запущен Deploy Butler, например `https://app.example/health`. `http://127.0.0.1:...` на VPS отсюда не виден.

Ожидаемые коды по умолчанию: 200, 301, 302. Редиректы не преследуются, поэтому 301 можно принять как успех. Текст ищется в теле ответа.

## Пример: pm2

Режим «релизы и симлинк current». Сервис должен работать из `/opt/myapp/current`, а этот путь — симлинк.

```bash
sudo mkdir -p /opt/myapp/releases
sudo chown -R myapp:myapp /opt/myapp
```

`app_dir` = `/opt/myapp/current`. Помощник распакует релиз в `/opt/myapp/releases/<имя>` и сделает `ln -sfn`. Откат переключает симлинк обратно. Каталог старого релиза остаётся.

```bash
cd /opt/myapp/current
pm2 start server.js --name myapp
pm2 save
```

В проекте: `service_type=pm2`, `service_name=myapp`. Перезапуск — `pm2 restart myapp`, проверка — `pm2 pid`. Журнал для проверок берётся как `pm2 logs --nostream`. У systemd это `journalctl` за последние N секунд.

Ключ SSH — файл без парольной фразы. Агент и пароль приложение не использует. При первом подключении сверьте отпечаток SHA256 и подтвердите его. Если ключ хоста сменился, панель снова спросит и не подключится, пока вы не подтвердите новый отпечаток.

## Что происходит при деплое

1. Проверка настроек и ключа. SSH, таймаут 10 с. Иначе текст из «сервер недоступен» и статус «Ошибка до изменений».
2. Папка собирается в tar.gz (пути относительные, uid/gid 0). Архив перепаковывается так же, после проверки содержимого.
3. Загрузка и сверка sha256. Не совпало — стоп, каталог приложения не менялся.
4. Бэкап. Не вышел — стоп, приложение не менялось. Галочка «Бэкап БД» (включена, если заданы пути) вызывает `sqlite3 .backup`.
5. Применение и сверка sha256 каждого файла. Расхождение — откат.
6. Перезапуск. Сервис не `active` — откат.
7. Журнал без шаблонов ошибок, health (таймаут 10 с, 3 попытки через 5 с), затем пауза 15 с и снова проверка, что сервис active. Провал — откат, перезапуск и повтор проверок. Статус «Откачено». Второй откат сам не запускается.
8. Архив удаляется из `/tmp`. В историю пишутся статус, sha, длительность и кто запустил.

Ручной откат из истории подтверждается отдельно, проходит те же проверки и при их провале не откатывается ещё раз.

## FAQ

**Это ходит в GitHub?** Нет.

**Почему помощник не ставится сам?** Чтобы у приложения не было произвольного root-шелла. Вы читаете скрипт и ставите его сами.

**Windows, macOS, Linux?** Панель — да. Paramiko, без rsync. Помощник рассчитан на Linux-сервер.

**Кнопка «Подтвердить» серая.** В обновлении запрещённый файл, `..`, абсолютный путь или ссылка, либо сервер не отдал хэши, либо ключ хоста ещё не подтверждён.

**Где история?** В локальной SQLite, в каталоге данных. На сервере лежат только бэкапы и, в режиме релизов, каталоги `releases/`.

**Ключ с паролем?** Не подходит. Снимите фразу или сделайте отдельный ключ для деплоя. Пароль нигде не спрашивается.

**Можно ли открыть панель в сеть?** Нет. Слушается только 127.0.0.1, проверяются `Host` и `Origin`, на изменения нужен CSRF-токен.

---

# Deploy Butler (English)

A local panel that deploys to your own VPS. No GitHub, no rsync, no sudo password. You install a small bash helper yourself. The app only uploads an archive over SFTP, checks its sha256, and runs `sudo` on that helper.

## Install

Python 3.11+. The server needs Linux with bash 4.4+, GNU tar, coreutils (`sha256sum`, `flock`), and `sqlite3` if you back up databases. systemd needs `systemctl`. pm2 needs `pm2` on root's `PATH`.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m deploy_butler
```

The panel binds only to `http://127.0.0.1:8787` and opens a browser. `DEPLOY_BUTLER_NO_BROWSER=1` skips the browser. `DEPLOY_BUTLER_PORT` changes the port (default 8787). Data directory:

- Linux: `~/.local/share/deploy-butler/`
- macOS: `~/Library/Application Support/deploy-butler/`
- Windows: `%APPDATA%\deploy-butler\`

Override with `DEPLOY_BUTLER_HOME`. That folder holds `butler.db`, `known_hosts` and local lock files. The SSH key contents and the sudo password are never stored.

Tests: `pip install -r requirements-dev.txt` then `pytest`. Checking the helper needs `shellcheck`.

## Using it

1. New project. The name is letters, digits, `.`, `_`, `-`. The helper and the sudoers file are named from it.
2. Settings. Updates folder, format (subfolder or tar/zip), SSH, app directory, service, backups. Check only verifies the folder. Export / import JSON moves settings without secrets: the key field is a path, not the key.
3. Prepare the server. Download the two files and run the steps as root on the VPS. The app does not install them.
4. Updates. The list refreshes itself. Open a release, read the file list, press Confirm.
5. The end of a run is a large Success, Rolled back, or Error. The step log streams to the browser. History keeps the log and can restore the state from before a chosen deploy.

An update is a subfolder (`r35`, `r36-fix`) or a `.tar.gz` / `.tgz` / `.tar` / `.zip` archive. The release name must match `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`.

Deploy is blocked when the update contains a forbidden file (by default `.env`, keys, databases, `node_modules/`, `.git/`, tests and seeds), an absolute path, `..`, a symlink, or a hard link inside an archive.

## Prepare the server

Helper: `/usr/local/sbin/deploy-butler-<project>`, owner `root:root`, mode `0755`.

sudoers: `/etc/sudoers.d/deploy-butler-<project>`, mode `0440`, one command and no wildcards:

```
deploy ALL=(root) NOPASSWD: /usr/local/sbin/deploy-butler-myapp
```

The tab also lists the checks and the removal. Short form:

```bash
sudo install -o root -g root -m 0755 deploy-butler-myapp /usr/local/sbin/deploy-butler-myapp
sudo install -o root -g root -m 0440 deploy-butler-myapp.sudoers /etc/sudoers.d/deploy-butler-myapp
sudo visudo -cf /etc/sudoers.d/deploy-butler-myapp
sudo visudo -c
sudo -n -u deploy sudo -n /usr/local/sbin/deploy-butler-myapp list-backups
```

Removal: delete the sudoers file, then the script, then `sudo visudo -c` again.

The helper accepts only `hashes`, `backup`, `apply`, `restart`, `status`, `logs`, `rollback`, `list-backups` and `prune`. Parallel runs take `flock`. The release name is checked. The archive must live in `/tmp/deploy-butler/<project>/`. Before extract, absolute paths, `..`, symlinks and hard links are rejected. The script does not print env files or secrets.

On the server the app does three things: SFTP into `/tmp/deploy-butler/<project>/`, `sha256sum` of the uploaded archive, and `sudo -n deploy-butler-<project> <subcommand>`. There is no arbitrary shell. The sudo password is never typed.

Old backups and releases are not deleted automatically. "Clean, keeping the last N" asks for confirmation and never deletes the live release.

## Example: Node and systemd

App directory `/opt/myapp/app`, user `myapp`, service `myapp`. Deploy mode: in place.

`/etc/systemd/system/myapp.service`:

```ini
[Unit]
Description=myapp
After=network.target

[Service]
User=myapp
Group=myapp
WorkingDirectory=/opt/myapp/app
ExecStart=/usr/bin/node /opt/myapp/app/server.js
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now myapp
```

In the project: `service_type=systemd`, `service_name=myapp`, `app_owner=myapp:myapp`, `app_dir=/opt/myapp/app`. The health URL is requested from the computer running Deploy Butler, for example `https://app.example/health`. A `http://127.0.0.1:...` URL on the VPS is not reachable from here.

Default expected codes are 200, 301 and 302. Redirects are not followed, so a 301 can be a successful check. Expected text is searched in the body.

## Example: pm2

Deploy mode: releases and a `current` symlink. The process must run from `/opt/myapp/current`, and that path must be a symlink.

```bash
sudo mkdir -p /opt/myapp/releases
sudo chown -R myapp:myapp /opt/myapp
```

Set `app_dir` to `/opt/myapp/current`. The helper unpacks into `/opt/myapp/releases/<name>` and runs `ln -sfn`. Rollback points the symlink back. The old release directory stays.

```bash
cd /opt/myapp/current
pm2 start server.js --name myapp
pm2 save
```

In the project: `service_type=pm2`, `service_name=myapp`. Restart is `pm2 restart myapp`. Active means `pm2 pid` is a positive number. The log check uses `pm2 logs --nostream`. systemd uses `journalctl` for the last N seconds.

The SSH key must have no passphrase. The app does not use an agent or a password. On the first connection, compare the SHA256 fingerprint and confirm it. If the host key changes, the panel asks again and will not connect until you confirm the new fingerprint.

## What a deploy does

1. Validate settings and the key. SSH timeout is 10 seconds. Otherwise the unreachable hint is shown and the status is "Error before changes".
2. A folder is packed into tar.gz (relative paths, uid/gid 0). An archive is repacked the same way after its contents are checked.
3. Upload and compare sha256. A mismatch stops before the app directory changes.
4. Backup. If it fails, the app is left untouched. "Back up databases" (on when paths are set) runs `sqlite3 .backup`.
5. Apply, then compare each file's sha256. A mismatch rolls back.
6. Restart. If the service is not `active`, roll back.
7. Logs must not match the error patterns. Health uses a 10 second timeout, 3 attempts, 5 seconds apart. After another 15 seconds the service must still be active. A failure rolls back, restarts, and runs the checks again. Status: Rolled back. There is no second automatic rollback.
8. The archive is removed from `/tmp`. History stores the status, sha, duration and who started it.

A manual rollback from history asks for confirmation, runs the same checks, and does not roll back again if those checks fail.

## FAQ

**Does it talk to GitHub?** No.

**Why isn't the helper installed for me?** So the app never has an arbitrary root shell. You read the script and install it.

**Windows, macOS, Linux?** The panel, yes. Paramiko, no rsync. The helper targets a Linux server.

**Confirm is disabled.** The update has a forbidden file, `..`, an absolute path or a link, or the server did not return hashes, or the host key is not confirmed yet.

**Where is history?** Local SQLite, in the data directory. The server keeps backups and, in releases mode, the `releases/` directories.

**A key with a passphrase?** Not supported. Remove the passphrase or create a deploy key. A password is never requested.

**Can the panel be exposed to the network?** No. It binds to 127.0.0.1 only, checks `Host` and `Origin`, and mutating requests need a CSRF token.
