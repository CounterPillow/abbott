# encoding: UTF-8
from __future__ import division
import random
from collections import defaultdict, deque
from functools import wraps
import datetime
import time
import bisect

from twisted.internet import reactor, defer
from twisted.python import log

from .admin import OpError, OpTimedOut, NoOpMethod

try:
    from pretty import date as prettydate
except ImportError:
    print "Please install the pypi package 'py-pretty'"
    raise

from ..command import CommandPluginSuperclass
from ..transport import Event

def find_time_until(hour_minute):
    """Returns a datetime.timedelta for the time interval between now and the
    next time of the given hour, in the current locale

    """
    today = datetime.date.today()

    hour = datetime.time(hour=hour_minute[0], minute=hour_minute[1])

    targetdt = datetime.datetime.combine(today, hour)
    if targetdt <= datetime.datetime.now():
        tomorrow = today + datetime.timedelta(days=1)
        targetdt = datetime.datetime.combine(tomorrow, hour)

    timeuntil = targetdt - datetime.datetime.now()
    return timeuntil

def td_to_str(td):
    """Takes a timedelta and returns a string describing the interval as if it
    were taking place at a point in the future from now

    """
    return prettydate(
            datetime.datetime.now() + td
            )

def delay(t):
    """"This should only be used for short delays because long delays won't be
    canceled properly on plugin unload or other exceptional cases

    """
    d = defer.Deferred()
    reactor.callLater(t, d.callback, None)
    return d

def require_channel(func):
    """Wraps command callbacks and requires them to be in response to a channel
    message, not a private message directed to the bot.

    """
    @wraps(func)
    def newfunc(self, event, match):
        if event.direct:
            event.reply("Hey, you can't do that in here!")
        else:
            return func(self, event, match)
    return newfunc

# http://code.activestate.com/recipes/577363-weighted-random-choice/
def weighted_random_choice(seq, weight):
    """Returns a random element from ``seq``. The probability for each element
    ``elem`` in ``seq`` to be selected is weighted by ``weight(elem)``.

    ``seq`` must be an iterable containing more than one element.

    ``weight`` must be a callable accepting one argument, and returning a
    non-negative number. If ``weight(elem)`` is zero, ``elem`` will not be
    considered. 
        
    """ 
    weights = 0
    elems = [] 
    for elem in seq:
        w = weight(elem)     
        try:
            is_neg = w < 0
        except TypeError:    
            raise ValueError("Weight of element '%s' is not a number (%s)" %
                             (elem, w))
        if is_neg:
            raise ValueError("Weight of element '%s' is negative (%s)" %
                             (elem, w))
        if w != 0:               
            try:
                weights += w
            except TypeError:
                raise ValueError("Weight of element '%s' is not a number "
                                 "(%s)" % (elem, w))
            elems.append((weights, elem))
    if not elems:
        raise ValueError("Empty sequence")
    ix = bisect.bisect(elems, (random.uniform(0, weights), None))
    return elems[ix][1]


class VoiceOfTheDay(CommandPluginSuperclass):
    def __init__(self, *args):
        self.started = False
        self.timer = None
        
        # Keeps track of the last x times that the !odds command was issued.
        # Will only do so many in a minute to prevent spam
        self.last_odds = deque(maxlen=4)

        super(VoiceOfTheDay, self).__init__(*args)

    def start(self):
        super(VoiceOfTheDay, self).start()

        self.listen_for_event("irc.on_nick_change")

        votdgroup = self.install_cmdgroup(
                grpname="votd",
                permission="vott.configure",
                helptext="Voice of the Day configuration commands",
                )

        votdgroup.install_command(
                cmdname="enable",
                callback=self.enable,
                helptext="Turns on votd for this channel",
                )
        votdgroup.install_command(
                cmdname="disable",
                callback=self.disable,
                helptext="Turns off votd for this channel",
                )

        votdgroup.install_command(
                cmdname="draw",
                callback=self.draw,
                helptext="Draws the raffle right now",
                )

        votdgroup.install_command(
                cmdname="settime",
                cmdusage="<hour:minute>",
                argmatch=r"(?P<hour>\d+)(?:[:](?P<minute>\d+))$",
                callback=self.settime,
                helptext="Sets which hour the drawing will happen, in the current locale",
                )

        self.install_command(
                cmdname="transfer",
                cmdusage="<nick>",
                argmatch="(?P<nick>[^ ]+)$",
                callback=self.transfer,
                helptext="if you are the VOTD, transfer it to another",
                permission=None,
                )

        self.install_command(
                cmdname="odds",
                cmdusage="[nick]",
                argmatch="(?P<user>[^ ]+)?$",
                callback=self.check_prob,
                helptext="Check your odds for winning voice of the day",
                permission=None,
                )

        # Don't forget!
        self._set_timer()
        self.started = True

        # last spoken time keeps track of channel idleness. We don't want to
        # interrupt conversation, so we see when the last time anyone spoke and
        # will only do a drawing if nobody is talking
        self.lastspoken = 0

    def stop(self):
        if self.timer:
            self.timer.cancel()

        self.started = False

    def reload(self):
        super(VoiceOfTheDay, self).reload()

        # maps nicks to counts
        self.config["counter"] = defaultdict(int, self.config.get("counter", {}))

        # The channel we're doing this in, or None for disabled
        self.config['channel'] = self.config.get('channel', None)

        # The hour of the day to do the drawing
        self.config['hour'] = tuple(self.config.get('hour', (0,0)))

        # Who currently has voice due to this plugin, right now. Will be
        # devoiced at the next drawing
        self.config['currentvoice'] = self.config.get("currentvoice", None)

        # The multipliers get multiplied by vote totals before the drawing
        self.config['multipliers'] = defaultdict(lambda: 0.01, self.config.get("multipliers", {}))

        self.config['scalefactor'] = self.config.get("scalefactor", 100)

        # reset the timer, in case the hour in the config was changed manually
        if self.started:
            self._set_timer()

        old_save = self.config.save
        def add_probs_and_save():
            # Add the probability to the saved config for convenience of
            # external apps that may want to read this data but not have
            # to calculate the odds themselves
            total = 0
            self.config['chance'] = {}
            for name, count in self.config['counter'].iteritems():
                ecount = count * self.config['multipliers'][name] * self.config['scalefactor']
                ecount = int(ecount)
                self.config['chance'][name] = ecount
                total += ecount
            if total:
                for name, ecount in self.config['chance'].items():
                    self.config['chance'][name] = ecount / total
            old_save()
        self.config.save = add_probs_and_save

    def _set_timer(self):
        if self.timer:
            self.timer.cancel()
            self.timer = None

        channel = self.config["channel"]
        if channel:
            timeuntil = find_time_until(self.config['hour'])
            self.timer = reactor.callLater(
                    max(int(timeuntil.total_seconds()), 5),
                    self._timer_up,
                    )
        
    @require_channel
    def enable(self, event, match):
        channel = event.channel
        self.config["channel"] = channel
        self.config.save()
        self._set_timer()
        event.reply("Done. Next scheduled drawing is {0}".format(
            td_to_str(
                find_time_until(self.config['hour'])
            )))

    @require_channel
    def disable(self, event, match):
        channel = event.channel
        self.config["channel"] = None
        self.config.save()
        self._set_timer()
        event.reply("Voice of the Day disabled for {0}".format(channel))

    @require_channel
    def settime(self, event, match):
        channel = event.channel
        hour = int(match.groupdict()['hour'])
        minute = int(match.groupdict().get('minute', None) or 0)
        if hour < 0 or hour > 23:
            event.reply("What kind of hour is that?")
            return
        if minute < 0 or minute > 59:
            event.reply("What kind of minute is that?")
            return
        self.config['hour'] = (hour, minute)
        self.config.save()
        self._set_timer()

        event.reply("VOTD drawing {3} happen at {0}:{1}, which is {2}".format(
            hour, minute,
            td_to_str(find_time_until((hour,minute))),
            "will" if self.config['channel'] else "would",
            ))


    @defer.inlineCallbacks
    def _send_as_op(self, event, reply=lambda s: None):
        """Issues an ircadmin.opself request, then sends the event. If the
        opself fails, sends an error to the reply function provided

        """
        try:
            yield self.transport.issue_request("ircadmin.opself", event.channel)
        except OpTimedOut:
            reply("I could not become OP. Check the error log, configuration, etc.")
            defer.returnValue(False)
        except NoOpMethod:
            reply("I can't do that in %s, I don't have OP and have no way to acquire it!" % event.channel)
            defer.returnValue(False)
        else:
            self.transport.send_event(event)
            defer.returnValue(True)

    def _timer_up(self):
        self.timer = None

        if not self.started:
            # Maybe the plugin was unloaded?
            return

        IDLE_TIME = 60*5
        IDLE_TIME = self.config.get("idle_time", 60*5)

        now = time.time()
        if now - self.lastspoken >= IDLE_TIME:
            self._do_votd()
        else:
            towait = IDLE_TIME - (now - self.lastspoken)
            log.msg("Was going to do VOTD but the channel is active. Waiting %s seconds and trying again"%towait)
            self.timer = reactor.callLater(towait, self._timer_up)

    @defer.inlineCallbacks
    def _do_votd(self, channel=None):
        # Assume the timer is already None or has fired to call this method
        channel = self.config["channel"] or channel
        if not channel:
            raise RuntimeError("_do_votd() was called, but no channel defined")
        log.msg("Doing VOTD for %s" % channel)
        def say(msg):
            self.transport.send_event(Event("irc.do_msg",
                user=channel,
                message=msg,
                ))

        names = (yield self.transport.issue_request("irc.names", channel))

        # de-voice the current voice if he/she still has it
        currentvoice = self.config.get("currentvoice")
        self.config['currentvoice'] = None

        if currentvoice:
            if "+"+currentvoice in (names):
                e = Event("irc.do_mode",
                        channel=channel,
                        set=False,
                        modes="v",
                        user=currentvoice,
                        )
                if not (yield self._send_as_op(e)):
                    say("I was going to do Voice of the Day, but there was an error. someone halp plz!")
                    log.msg("Error while un-voicing previous voice. Bailing")
                    return
        else:
            # Gain OP here, if we didn't gain it from the previous voice.
            try:
                yield self.transport.issue_request("ircadmin.opself", channel)
            except OpError:
                say("I was going to do Voice of the Day, but there was an error. someone halp plz!")
                return



        # Take a copy of the counters and use that for the rest of the method
        counter = self.config["counter"]
        self.config['counter'] = defaultdict(int, counter)

        # Half the counter for everyone for next time. If an entry goes to 0,
        # remove it
        for i, c in self.config['counter'].items():
            if c <= 1:
                del self.config['counter'][i]
            else:
                self.config['counter'][i] = c // 2

        # Prune out the multipliers dict. Users that don't have an entry in the
        # counter dict are removed
        for user in self.config['multipliers'].keys():
            if user not in self.config['counter']:
                del self.config['multipliers'][user]

        # don't count any user that isn't actually here, and users that already
        # have voice or op
        names = set(
                x for x in names if not x.startswith("@") and not x.startswith("+")
                )
        for contestant in counter.keys():
            if contestant not in names:
                del counter[contestant]

        try:
            winner = weighted_random_choice(counter.iterkeys(),
                    lambda user: int(
                        counter[user] *
                        self.config['multipliers'][user] *
                        self.config['scalefactor']
                        )
                    )
        except ValueError:
            say("I was going to do the voice of the day, but nobody seems to be eligible =(")
            self.config.save()
            return


        say("Ready everyone? It's time to choose a new Voice of the Day!")
        yield delay(3)

        # Adjust all the multipliers up
        for user, m in self.config['multipliers'].items():
            # (except the winner)
            if user != winner:
                self.config['multipliers'][user] = min(1.0, m*1.5)
            else:
                self.config['multipliers'][user] = m*0.01

        self.config.save()

        say("...aaaaand the winner is....")
        yield delay(1)
        say("{0}!".format(winner))

        yield delay(1)
        yield self._send_as_op(Event("irc.do_mode",
            channel=channel,
            set=True,
            modes="v",
            user=winner,
            ))
        self.config['currentvoice'] = winner
        yield delay(5)
        say("until next time...")

        self._set_timer()
        

            
    def draw(self, event, match):
        channel = event.channel
        if self.config['channel'] and self.config['channel'] != channel:
            event.reply("I can only do that in {0}".format(self.config['channel']))
        else:
            if self.timer:
                self.timer.cancel()
                self.timer = None
            self._do_votd(channel)

    @defer.inlineCallbacks
    def on_event_irc_on_privmsg(self, event):
        super(VoiceOfTheDay, self).on_event_irc_on_privmsg(event)

        self.lastspoken = time.time()

        if getattr(event, "_was_odds", False):
            return

        # This delay is a bit of a hack. If we do e.g. a configreload, and this
        # handler happens to run before the reload, this will save the config,
        # clobbering the new one. So here we wait a second to let the other
        # handler run, reload our config, THEN we increment the counter.
        yield delay(1)
        if event.channel == self.config["channel"]:
            nick = event.user.split("!")[0]
            self.config["counter"][nick] += 1
            self.config.save()

    def on_event_irc_on_nick_change(self, event):
        oldnick = event.oldnick
        newnick = event.newnick

        if self.config["currentvoice"] and self.config["currentvoice"] == oldnick:
            self.config["currentvoice"] = newnick
            self.config.save()

    @defer.inlineCallbacks
    def transfer(self, event, match):
        target = match.groupdict()['nick']
        channel = self.config['channel']
        if channel != event.channel:
            event.reply("I'm not doing votd in this channel. This command only works in " + channel)
            return

        requestor = event.user.split("!")[0]
        if self.config["currentvoice"] != requestor:
            event.reply("You are not the VOTD. Get out of here, you!")
            return

        names = (yield self.transport.issue_request("irc.names", channel))
        if "+"+requestor not in names:
            event.reply("Hey, where'd your hat go?")
            return

        if "+"+target in names:
            event.reply("{0} already has voice".format(target))
            return

        if "@"+target in names:
            event.reply("no can do")
            return

        if target not in names:
            event.reply("who?")
            return

        event.reply("okay...")

        e = Event("irc.do_mode",
                channel=channel,
                set=False,
                modes="v",
                user=requestor,
                )
        if not (yield self._send_as_op(e)):
            log.msg("Error while un-voicing previous voice. Bailing")
            return
        e = Event("irc.do_mode",
                channel=channel,
                set=True,
                modes="v",
                user=target,
                )
        self._send_as_op(e)
        self.config["currentvoice"] = target
        self.config.save()

    def check_prob(self, event, match):
        event._was_odds = True
        user = match.groupdict()['user']

        self.last_odds.append(time.time())
        if len(self.last_odds) == self.last_odds.maxlen and time.time() - self.last_odds[0] < 60:
            reply_opts = {"notice": True, "direct": True}
        else:
            reply_opts = {}

        if not user:
            user = event.user.split("!")[0]
            msg = "Your chance of winning the next VOTD drawing is"
            self.config["counter"][user] = max(self.config["counter"][user] - 1, 0)
            self.config.save()
        else:
            msg = u"{0}’s chance of winning the next VOTD is".format(user)

        if user not in self.config['counter']:
            return

        all_counts = []
        total = 0
        for name, count in self.config['counter'].iteritems():
            # compute the effective entry count
            ecount = count * self.config['multipliers'][name] * self.config['scalefactor']
            ecount = int(ecount)
            total += ecount

        my_ecount = self.config['counter'][user] * self.config['multipliers'][user] * self.config['scalefactor']
        my_chances = int(my_ecount) / total * 100
        event.reply(u"{1} {0:.2f}% with {2} entries and a multiplier of {3:.3f}".format(my_chances, msg, self.config['counter'][user], self.config['multipliers'][user]),
                **reply_opts)
