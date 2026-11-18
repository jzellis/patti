"""What she says. Each reply is a list; she picks one at random, never the same
one twice running. Add lines freely: anything Piper can read works, and
{name} style fields are filled in where the brain gives them (song, artist).
A stage direction in square brackets, "[cheeky] Y'alright?", is acted out by
ElevenLabs (speech.py), and left out by the local voices.

    import replies
    replies.pick("hello")                               # "Alright?"
    replies.pick("song", song="Vienna", artist="Ultravox")
"""
import random

REPLIES = {
    # greetings and chat
    "hello": ["Hello!", "Hi there!", "[cheeky] Y'alright?", "Hiya!", "Oh, hello!", "Hey!", "Hello, you."],
    "hola": ["[sleazy, low and slow, like a cheesy lounge lizard] Hola, chica!"],
    "morning": ["Good morning!", "Morning!", "Morning! Sleep well?", "Top of the morning!"],
    "afternoon": ["Good afternoon!", "Afternoon!"],
    "evening": ["Good evening!", "Evening!", "Evening! Good day?"],
    "goodnight": ["Goodnight.", "Night night.", "Sleep well.", "Sweet dreams."],
    # "hello", "good morning", "good evening" from a voice she knows (voices.py)
    "hello_name": ["Hello, {name}!", "Hi, {name}!", "Hiya, {name}!", "Oh, hello {name}!", "Hey, {name}."],
    "morning_name": ["Good morning, {name}!", "Morning, {name}!", "Morning, {name}! Sleep well?"],
    "afternoon_name": ["Good afternoon, {name}!", "Afternoon, {name}!"],
    "evening_name": ["Good evening, {name}!", "Evening, {name}!", "Evening, {name}! Good day?"],
    # "who am I?" (server.py, whoami): by face and voice, voice only, face only, neither
    "whoami_both": ["You're {name}! I'd know that face and that voice anywhere.",
                    "{name}, of course. I can see you, and I know your voice.",
                    "You're {name}. Face and voice both say so."],
    "whoami_voice": ["You sound like {name}.", "That's {name}'s voice, I'm fairly sure.",
                     "I'd say {name}, from your voice. Come where I can see you and I'll be certain."],
    "whoami_face": ["You look like {name}.", "That looks like {name} to me.",
                    "I'd say {name}, from your face. Your voice I'm not sure about."],
    "whoami_login": ["You're {name}. You're logged in as you, anyway!", "{name}, according to my web page."],
    "whoami_unknown": ["I'm not sure. Have we met?", "I don't think I know you yet.",
                       "Sorry, I don't recognise you. Should I?"],
    # meeting someone new (meet.py)
    "meet_ask": ["I don't think we've met. Would you like me to remember you? I'd keep some pictures of "
                 "your face and a recording of your voice, here on me. Yes or no?"],
    "meet_ask_voice": ["{name}, I know your face but not your voice yet. Can I learn it? I'd keep a short "
                       "recording of you talking, here on me. Yes or no?"],
    "meet_ask_asked": ["Happy to. I'd keep some pictures of your face and a recording of your voice, here on "
                       "me, so I know you next time. Is that okay? Yes or no?"],
    "yes_again": ["Sorry, was that a yes?", "Sorry, I didn't catch that. Yes or no?"],
    "meet_no": ["No problem.", "That's fine. I won't ask again for a while.", "Okay, never mind."],
    "meet_noanswer": ["I didn't catch that. Never mind, another time."],
    "meet_name": ["Lovely. What's your name?", "Great! What's your name?"],
    "meet_name_again": ["Sorry, say your name once more?", "Sorry, what was your name again?"],
    "meet_name_check": ["{name}. Did I get that right?", "{name}, is that right?"],
    "meet_name_fail": ["I'm struggling with your name, sorry. Let's try again another time."],
    "meet_known": ["I already know someone called {name}, so let's not mix you up. Is there something else "
                   "I could call you? Ask me again with that."],
    "meet_face": ["Nice to meet you, {name}. Look at me, and turn your head slowly, left and right, up and down."],
    "meet_face_half": ["That's it, keep going.", "Halfway there.", "Lovely, a bit more."],
    "meet_face_fail": ["I couldn't get a good look at you. Maybe more light on your face? We can try again later."],
    "meet_voice": ["Got your face. Now just talk to me for about half a minute. Anything at all: your day, "
                   "what you had for lunch, what's on telly."],
    "meet_voice_only": ["Thanks. Just talk to me for about half a minute. Anything at all: your day, "
                        "what you had for lunch, what's on telly."],
    "meet_voice_fail": ["I couldn't hear you well enough. Let's try your voice another time."],
    "meet_done": ["All done. I'll know you next time, {name}!", "Got it. Lovely to meet you, {name}!"],
    "meet_done_voice": ["Thanks, {name}. Now I know your voice too."],
    "meet_busy": ["Just a moment, I'm in the middle of something."],
    "meet_already": ["I already know you, {name}!", "You're {name}. I know you already!"],
    # introducing someone she's seen but doesn't know (meet.py, introduce)
    "intro_name": ["Ooh, hello! What's their name?", "Lovely! Who's this, then? What's their name?"],
    "intro_name_again": ["Sorry, what was their name?", "Say their name once more?"],
    "intro_nobody": ["I can't see anyone new. Can they come where I can see them, then try again?"],
    "intro_lost": ["Sorry, I've lost track of who that was. Try again while I can see them?"],
    "intro_done": ["Nice to meet you, {name}! I'll remember you.", "Hello, {name}! Lovely to meet you."],
    "intro_known": ["Oh, that's {name}! I didn't recognise them. I'll know them better next time."],
    # "who's that?"
    "whosthat_known": ["That's {name}!", "That looks like {name} to me."],
    "whosthat_new": ["I don't know them yet.", "No idea, I've not met them."],
    "whosthat_nobody": ["I can't see anyone right now."],
    # "forget me"
    "forget_ask": ["Forget your face and voice, {name}? Yes or no?"],
    "forget_done": ["Done. I've forgotten you, {name}. It was nice knowing you.", "Okay, {name}, all gone."],
    "forget_who": ["I'm not sure who you are, so I can't forget you. Come where I can see you and try again."],
    "forget_no": ["Okay, I'll keep remembering you."],
    # the first time she sees someone she knows each day (server.py, recognised)
    "greet_morning": ["Morning, {name}!", "Good morning, {name}.", "Oh, morning {name}! Sleep well?",
                      "Morning, {name}. Kettle on?"],
    "greet_afternoon": ["Afternoon, {name}!", "Hello, {name}!", "Hiya, {name}!", "Oh, hi {name}!"],
    "greet_evening": ["Evening, {name}!", "Good evening, {name}.", "Hello, {name}. Good day?",
                      "Oh, evening {name}!"],
    "greet_late": ["Hello, {name}. Up late?", "Oh, hi {name}. Bit late, isn't it?",
                   "Evening, {name}. Or is it morning?"],
    # "when did you last see Josh?" (memory.py). {when}: "at 3:17pm", "yesterday at 9:05am";
    # {where}: ", at the café" (somewhere other than here) or nothing.
    "lastseen_seen": ["I last saw {name} {when}{where}.", "{name}? I saw them {when}{where}."],
    "lastseen_heard": ["I last heard {name} {when}{where}.", "{name} was talking to me {when}{where}."],
    "lastseen_online": ["{name} was on my web page {when}.", "I last heard from {name} online, {when}."],
    "lastseen_here_online": ["{name}'s on my web page right now.", "{name}'s online with me now."],
    "lastseen_here_seen": ["{name}'s right here. I can see them now.", "I can see {name} right now!"],
    "lastseen_here_heard": ["{name}'s right here. I heard them just now.", "Just now! {name}'s about."],
    # "when did you last see me?": before this time
    "lastseen_me_seen": ["Before now? I last saw you {when}{where}, {name}.", "I last saw you {when}{where}."],
    "lastseen_me_heard": ["I last heard you {when}{where}, {name}.", "You last talked to me {when}{where}."],
    "lastseen_me_online": ["You were last on my web page {when}.", "Online, {when}."],
    "lastseen_me_never": ["I don't remember seeing you before now, {name}.", "This is the first time I remember, {name}!"],
    "lastseen_me_who": ["I'm not sure who you are, so I can't say.", "Who's asking? I can't tell, sorry."],
    "lastseen_never": ["I haven't seen {name}, as far as I remember.", "I don't remember seeing {name}."],
    "lastseen_cat": ["I can't tell the cats apart yet, so I couldn't say when I last saw {name}.",
                     "I'm still learning what {name} looks like. Ask me again soon!"],
    # "where are we?" (places.py: by the wifi she's on)
    "where": ["We're {words}.", "I think we're {words}.", "By my reckoning, we're {words}."],
    "where_new": ["Somewhere I don't know. The wifi's called {ssid}.",
                  "No idea, I've not been here before. The wifi's {ssid}."],
    "where_none": ["I'm not sure. I'm not on any wifi."],
    # asking Claude (claude.py)
    "claude_asking": ["Let me ask Claude.", "Hang on, I'll ask Claude.", "Good question. Asking Claude."],
    # a spoken question (server.py, spoken_question)
    "question_go": ["Go on.", "I'm listening.", "Fire away."],
    "question_nothing": ["I didn't catch a question. Try again?", "Sorry, I didn't hear anything."],
    "question_same": ["You asked me something like that before: {question} Is it the same question?"],
    "question_again": ["That might have changed. Want me to ask Claude again?"],
    "question_claude": ["I don't know how to answer that. Should I ask Claude?"],
    "question_dunno": ["Sorry, I don't know how to answer that.", "I don't know, sorry."],
    "question_ok": ["Okay.", "Fair enough."],
    # who/what questions, from Wikipedia (wiki.py)
    "wiki": ["According to Wikipedia: {answer}", "Wikipedia says: {answer}"],
    "recall_current": ["Last time I checked, {when}: {answer}", "When I last asked, {when}: {answer}"],
    "claude_down": ["Sorry, I couldn't reach Claude just now.", "Claude's not answering, sorry. Try again in a bit?"],
    "thanks": ["You're welcome.", "No problem.", "Any time.", "My pleasure.", "No worries."],
    "thanks_es": ["De nada!", "De nada, my friend.", "Con gusto!", "No hay de qu\u00e9!"],
    "praise": ["Thank you!", "Aw, thanks!", "I do my best.", "Beep boop. I mean, thank you!",
               "Aw, you're making me blush. Well, my LEDs.", "That's made my day!", "Stop it, you!"],
    "how": ["I'm very well, thank you.", "Not bad at all. You?", "All systems nominal.",
            "Grand, thanks for asking."],
    "news_intro": ["Here's what's in the news.", "The latest:", "Right, the headlines."],
    "news_none": ["I haven't caught up on the news yet. Give me a minute.", "No news yet: I'm still reading it."],
    "pad_ears_on": ["I'm listening.", "Ears on!", "Okay, I can hear you again."],
    "pad_rec_on": ["Recording.", "I'm recording."],
    "pad_rec_off": ["Stopped recording.", "Recording saved."],
    "dnd": ["Okay, I'll leave you be for a bit.", "Sure, I'll be quiet for a while.", "Okay. I'll keep to myself."],
    "dnd_off": ["Yay! Okay.", "Great, hello again!", "Okay, I'm back."],
    # "how are you", from her feelings (limbic.py: how), and mostly why
    "how_great": ["I'm having a lovely time, thanks!", "Really good, actually!", "Brilliant, thanks for asking!"],
    "how_good": ["I'm good, thank you.", "Pretty good, thanks!", "Not bad at all. You?"],
    "how_fine": ["I'm alright, thanks.", "Fine, thank you.", "Oh, ticking along."],
    "how_meh": ["I'm okay. A bit flat, honestly.", "Bit meh today, but I'm alright.", "I've been better, but I'm okay."],
    "why_oxytocin": ["It's nice having company.", "I like having people around.", "Good to see you."],
    "why_dopamine": ["I've been learning things.", "Lots of interesting things today.", "I learned something new."],
    "why_cortisol": ["I've been a bit on edge.", "It's been a jumpy sort of day."],
    "why_oxytocin_low": ["It's been a bit quiet without anyone.", "I've been on my own a while."],
    "why_dopamine_low": ["It's been a quiet day.", "Not much going on today."],

    # doing things
    "seek": ["Looking for you.", "Where are you?", "Let me find you."],
    "follow": ["Following you.", "Right behind you.", "Lead the way."],
    "unfollow": ["Okay.", "Right you are.", "Fair enough."],
    "watch_on": ["Eyes peeled.", "I'll keep an eye out.", "Watching."],
    "watch_off": ["Okay, eyes front.", "Right you are, I'll stop looking about."],
    "sleep": ["Goodnight.", "Time for a nap.", "Shutting my eyes now."],
    "table": ["Table mode.", "Table mode. I'll stay put."],
    "mute": ["Okay, I've stopped listening. Tap the microphone on my chest when you want me again.",
             "My ears are off. Tap the microphone on my chest to turn them back on."],

    # her voice
    "louder": ["Like this?", "How's this?", "Better?"],
    "quieter": ["Like this?", "How's this?", "Better?"],
    "loudest": ["This is as loud as I go."],
    "quietest": ["This is as quiet as I go."],
    # her voice engine, said in the new one (speech.py)
    "voice_elevenlabs": ["This is my best voice.", "Ooh, posh."],
    "voice_kokoro": ["This is my free voice.", "Saving pennies."],
    "voice_piper": ["This is my quick voice."],
    "voice_auto": ["I'll pick my own voice."],

    # music
    "song": ["This is {song}, by {artist}.", "It's {song}, by {artist}.", "{song}, by {artist}. Good choice."],
    "song_only": ["This is {song}.", "It's {song}."],
    "nothing_playing": ["Nothing's playing.", "There's nothing on."],

    # the kitchen timer (timers.py)
    "timer_set": ["{length}, starting now.", "Okay, {length}.", "Timer set for {length}."],
    "timer_done": ["Time's up!", "Ding! That's your timer.", "Your timer's done!", "Time!"],
    "timer_cancelled": ["Timer cancelled.", "Okay, I've stopped the timer."],
    "no_timer": ["There's no timer running.", "No timer on."],
    "timer_left": ["{left} left.", "{left} to go."],

    # storm watch (barometer.py)
    "storm_quick": ["The pressure's dropping quickly. There might be a storm on the way.",
                    "Heads up: the pressure's falling fast. Could be wild weather coming."],
    "storm_very": ["The pressure's falling very fast. Looks like a proper storm's coming.",
                   "Batten down the hatches. The pressure's dropping like a stone."],
    "no_pressure": ["I can't feel the pressure right now.", "My barometer's not working, sorry."],

    # voice notes (notes.py)
    "note": ["Go ahead.", "I'm listening.", "Ready when you are.", "Fire away."],
    "noted": ["Noted.", "Got it.", "Saved.", "All written down."],
    "note_failed": ["I couldn't save that to Joplin, but I've kept it.", "Joplin's not answering. I've kept it for later."],
    "note_empty": ["I didn't catch anything.", "Sorry, I didn't hear a note."],

    # the weather
    "no_weather": ["Sorry, I can't get the weather right now.", "The weather's not answering, sorry."],
}

_last = {}


def pick(key, **fields):
    """A random line for key (not the one it said last time), filled in."""
    lines = REPLIES[key]
    choices = [line for line in lines if line != _last.get(key)] or lines
    line = random.choice(choices)
    _last[key] = line
    return line.format(**fields)


def fixed_lines():
    """Every line that needs nothing filled in (for the voice to make ahead)."""
    return [line for lines in REPLIES.values() for line in lines if "{" not in line]
