# Stagehand

**This software is somewhat half-baked. It only works (though it works well)
if you have an [Easynews](http://easynews.com) account.  Generic NNTP isn't
supported yet (but help is welcome).**


## What it is

Stagehand is a manager for your favourite TV series.  It automatically
downloads new episodes of the TV shows in your library, and provides a convenient
interface to download previously aired episodes.

Here are some of the main features:

* ~~Pretty, modern-looking UI~~ (Well it was 5 years ago.)
* Support for multiple TV metadata providers (currently TheTVDB and TVRage): easily choose the authoritative provider per-series
* A Just Works design principle: no cumbersome setup or external tools
* Support for Easynews HTTP-based global search
* Multi-platform: tested on Linux and Windows (and theoretically works on OS X)



## What it isn't (yet)

The core of Stagehand is quite robust, but many essential features are missing
(but planned):

* NZB and NNTP support (for non-Easynews Usenet services): the most critical missing functionality
* Bittorrent
* Web-based configuration UI
* Ability to import an existing TV library
* ... and a bazillion FIXMEs and TODOs in the source



## What it looks like

![](https://helix.urandom.ca/stagehand/stagehand.jpg)

![](https://helix.urandom.ca/stagehand/stagehand2.jpg)



## How to install it

Stagehand is powered by Python and requires Python 3.3 or later.


### Windows

First [download Python](https://www.python.org/downloads/) and install it.

Then [download Stagehand](http://stagehand.ca/downloads/stagehand.pyw), which
you can run directly.

Once running, you should see a TV icon in your system tray.  Double clicking
it will open Stagehand in your browser.  You can also right-click the icon to
see additional options.



### Linux

If you have a relatively recent Linux distribution, you probably already have
Python 3.3+.  You can check at the command line:

```bash
$ python3 --version
Python 3.4.0
```

If you're running an older Ubuntu version, you might try the
[Old and New Python Versions PPA](https://launchpad.net/~fkrull/+archive/deadsnakes)
to install a newer version of Python alongside your system version.

#### The Easy Way

Just fetch the latest build as a single executable:

```bash
$ wget http://stagehand.ca/downloads/stagehand && chmod a+x stagehand
$ ./stagehand
```

It will output a line that looks like:

```
2014-06-18 22:58:02,571 [INFO] stagehand.web: started webserver at http://buffy:8088/
```

You should be able to browse to this URL (from inside your network,
presumably).


You can daemonize Stagehand if you want to run it in the background.  Logs go
to `~/.cache/stagehand/logs/`.  For debugging purposes, it's recommended you
run Stagehand with extra verbosity (`-vv`).

```bash
$ stagehand -vvb
```

#### The Hard Way

Stagehand installs from source like any other Python module:

```bash
$ git clone git://github.com/jtackaberry/stagehand.git
$ cd stagehand
$ sudo python3 setup.py install
$ stagehand
```


## How to configure it

Ideally you'd be able to configure Stagehand from the web interface, but this
isn't implemented yet (in spite of the fact that there is content in the
Settings section).

Until then, you will need to edit the config file
(`~/.config/stagehand/config` on Linux, or `%AppData%\Stagehand\config.txt` on
Windows).

Minimally, you will need these lines, which you can just append to the bottom
of the file:

```
searchers.enabled[+] = easynews
searchers.easynews.username = your_easynews_username
searchers.easynews.password = your_easynews_password
```

Once you save the config file, you're ready to start using Stagehand.  No reload
is needed, it will pick up the changes dynamically.



## Haven't you heard of $APP?

I realize there are several programs that do what Stagehand does, including and
especially the very popular Sick Beard.

There are a few reasons Stagehand exists:

* I needed an excuse to learn [CoffeeScript](http://coffeescript.org/)
* I wanted a project with which to learn Python 3.4's
  [asyncio](https://docs.python.org/3/library/asyncio.html) module
* I was a bit annoyed at Sick Beard's need for SABnzbd
* I suffer horribly from [NIH](http://en.wikipedia.org/wiki/Not_invented_here)

