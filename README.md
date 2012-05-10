# Stagehand

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

* Multi-platform: it is Linux-only right now
* NZB and NNTP support (for non-Easynews Usenet services): the most critical missing functionality
* Bittorrent
* Web-based configuration UI
* Ability to import an existing TV library
* ... and a bazillion FIXMEs and TODOs in the source



## What it looks like

![](https://helix.urandom.ca/stagehand/stagehand.jpg)



## How to install it

Stagehand is Linux software.

Assuming you have a relatively recent distro with a version of pip that can install
straight from git, installation should be fairly straightforward.

This is what's needed for Ubuntu or Debian.  Adapt as needed to your distro.

```bash
# Install base dependencies
$ sudo apt-get install python-dev python-beautifulsoup python-pycurl git

# Install kaa-base from git
$ sudo pip install -U git+https://github.com/freevo/kaa-base.git

# Install Stagehand from git
$ sudo pip install -U --no-deps git+https://github.com/jtackaberry/stagehand.git
```


## Haven't you heard of $APP?

I realize there are several popular programs that do what Stagehand does,
including and especially the very popular Sickbeard.

There are a few reasons Stagehand exists:

* I was a bit annoyed at Sickbeard's need for SABnzbd
* I suffer horribly from [NIH](http://en.wikipedia.org/wiki/Not_invented_here)
* to provide a real-world application to exercise the [Kaa application
  framework](https://github.com/freevo/kaa-base), which is another project of mine.

