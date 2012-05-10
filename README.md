# Stagehand

**Danger!  This software is half-baked. It's not released yet. If you want to try it,
expect some pain.**


## What it is

Stagehand is a manager for your favourite TV series.  It automatically
downloads new episodes of the TV shows in your library, and provides a convenient
interface to download previously aired episodes.

Here are some of the main features:

* Pretty, modern-looking UI
* Support for multiple TV metadata provider (currently TheTVDB and TVRage): easily choose the authoritative provider per-series
* A Just Works design principle: no cumbersome setup or external tools
* Support for Easynews HTTP-based global search



## What it isn't (yet)

The core of Stagehand is quite robust, but many essential features are missing
(but planned):

* NZB and NNTP support (for non-Easynews Usenet services): the most critical missing functionality
* Bittorrent
* Web-based configuration UI
* Ability to import an existing TV library
* Multi-platform: it is Linux-only right now
* ... and a bazillion FIXMEs and TODOs in the source



## What it looks like

![](https://helix.urandom.ca/stagehand/stagehand.jpg)



## How to install it

Stagehand is Linux software.

Assuming your distro has a relatively recent version of pip (0.5 or later),
installation should be fairly straightforward:

This is what's needed for Ubuntu or Debian.  Adapt as needed to your distro.

```bash
# Install base dependencies
$ sudo apt-get install python-dev python-beautifulsoup python-pycurl

# Install kaa-base from git
$ sudo pip install -U https://github.com/freevo/kaa-base/zipball/master

# Install Stagehand from git
$ sudo pip install -U --no-deps https://github.com/jtackaberry/stagehand/zipball/master
```

Once installed, run it:

```bash
$ stagehand -vv
```

It will output a line that looks like:

```
2012-05-10 00:02:13,011 [INFO] web: starting webserver at http://orion:8088/
```

You should be able to browse to this URL (from inside your network,
presumably).

Ideally you'd be able to configure Stagehand from the web interface, but this
isn't implemented yet (in spite of the fact that there is content in the Settings
section).  You will need to edit `~/.config/stagehand/config` which should be
fairly self-explanatory.  Minimally, you will need these lines:

```
searchers.enabled[+] = easynews
searchers.easynews.username = your_easynews_username
searchers.easynews.password = your_easynews_password
```

Once you save the config file, you're ready to start using Stagehand.  No reload
is needed; you should see on the console:

```
2012-05-10 00:07:11,433 [INFO] manager: config file changed; reloading
```

You can daemonize Stagehand if you want to run it in the background.  Logs go
to `~/.cache/stagehand/logs/`.  For debugging purposes, it's recommended you run
Stagehand with extra verbosity (`-vv`).

```bash
$ stagehand -vvb
```


## Haven't you heard of $APP?

I realize there are several popular programs that do what Stagehand does,
including and especially the very popular Sickbeard.

There are a few reasons Stagehand exists:

* I was a bit annoyed at Sickbeard's need for SABnzbd
* I suffer horribly from [NIH](http://en.wikipedia.org/wiki/Not_invented_here)
* to provide a real-world application to exercise the [Kaa application
  framework](https://github.com/freevo/kaa-base), which is another project of mine.

