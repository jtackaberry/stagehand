# Stagehand

**This software is somewhat half-baked. It only works (though it works well)
if you have an [Easynews](https://easynews.com) account.  Generic NNTP isn't
supported yet (but help is welcome).**


## What it is

Stagehand is a manager for your favourite TV series.  It automatically
downloads new episodes of the TV shows in your library, and provides a convenient
interface to download previously aired episodes.

Here are some of the main features:

* ~~Pretty, modern-looking UI~~ (Well it was 5 years ago.)
* Support for multiple TV metadata providers (currently TheTVDB and TVmaze): easily choose the authoritative provider per-series
* (Exclusive) support for Easynews HTTP-based global search
* Multi-platform: tested on Linux and Windows (and theoretically works on OS X)

## What it isn't

The core of Stagehand is quite robust, but many essential features are missing:

* NZB and NNTP support (for non-Easynews Usenet services): the most critical missing functionality
* Bittorrent
* Web-based configuration UI
* Ability to import an existing TV library
* ... and a bazillion FIXMEs and TODOs in the source


## What it looks like

![](https://stagehand.ca/img/stagehand.jpg)

![](https://stagehand.ca/img/stagehand2.jpg)



## How to run it

Stagehand is powered by Python and requires a Python version between 3.3 and 3.6.

This max Python version restriction is obviously problematic, but fixing it requires
nontrivial changes.  Consequently, we use a Docker image to ensure compatibility.

The Docker image is available at
[`jtackaberry/stagehand`](https://hub.docker.com/r/jtackaberry/stagehand).  This is the
simplest way to run it -- change `/data/tv` below with the path where you want to hold
downloaded episodes:

```bash
docker run -ti -u $UID:$UID --net=host -v $HOME:/stagehand -v /data/tv:/stagehand/tv jtackaberry/stagehand
```

If things are working properly, you should see output that looks like this:

```
2022-05-20 20:11:38,655 [INFO] stagehand: starting Stagehand 0.3.3
2022-05-20 20:11:38,662 [INFO] manager: watching /stagehand/.config/stagehand/config for changes
2022-05-20 20:11:38,680 [INFO] manager: scheduling next episode check for 2022-05-20 21:06:00
2022-05-20 20:11:38,682 [INFO] manager: checking for new episodes and availability
2022-05-20 20:11:38,692 [INFO] manager: no new episodes; we are all up to date
2022-05-20 20:11:38,696 [INFO] stagehand.web: started webserver at http://faith:8088/
2022-05-20 20:11:38,697 [INFO] manager: checking all epsiodes to see if any need resuming
2022-05-20 20:11:38,697 [INFO] manager: stagehand started, waiting for next new episodes check
```

Note the webserver URL in the output above.  You should be able to browse to this URL from
within your network.  Before proceeding with configuration, ensure that it's reachable.

👉 You can daemonize the container by replacing `-ti` in the command line above with `-d`.


## How to configure it

Ideally you'd be able to configure Stagehand from the web interface, but this isn't
implemented yet. Until then, you will need to edit the config file at
`~/.config/stagehand/config`.

Minimally, you will need these lines, which you can safely append to the bottom
of the file:

```
searchers.enabled[+] = easynews
searchers.easynews.username = your_easynews_username
searchers.easynews.password = your_easynews_password
```

Once you save the config file, you're ready to start using Stagehand.  No reload
is needed, it will pick up the changes dynamically.