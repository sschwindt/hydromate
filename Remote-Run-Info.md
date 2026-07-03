Yes, **possibly**, but **RDP is not the real fix**.

What is happening now: when you close Remmina’s **SSH session**, the SSH connection drops. The remote shell exits, and processes attached to that shell/terminal often receive `SIGHUP` and stop. That is normal.

With **RDP**, behavior is different:

* If you connect with RDP/xRDP and **disconnect** the RDP client, the graphical session usually stays alive on the Debian server.
* Commands running inside a terminal in that RDP desktop will usually continue.
* But if you **log out** of the RDP session, or xRDP/systemd is configured to kill disconnected sessions, those processes may stop.
* So RDP may help, but it is not guaranteed process persistence.

The proper solution is to detach your jobs from the SSH/RDP session.

Use one of these:

```bash
tmux new -s myjob
```

Run your command inside `tmux`, then detach with:

```bash
Ctrl+b
d
```

Later reconnect:

```bash
tmux attach -t myjob
```

For one-off commands:

```bash
nohup your_command > output.log 2>&1 &
```

or better on Debian/systemd systems:

```bash
systemd-run --user --scope your_command
```

For long simulations or hydraulic model runs, I would use `tmux` or `screen` for interactive runs, and `systemd-run`, a service file, or a scheduler for serious long jobs.

Bottom line: **RDP-disconnect will often keep commands running, SSH-close will usually kill attached commands, but `tmux`/`screen`/`nohup` is the correct fix.**

