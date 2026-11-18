"""Find chat bursts with several people reacting to the same moment."""

from __future__ import annotations

import collections
import re
import statistics
from dataclasses import dataclass, field


MESSAGE_RE = re.compile(r":(?P<user>[^! ]+)!.*? PRIVMSG #[^ ]+ :(?P<text>.*)")
REACTION_RE = re.compile(
    r"\b(?:lol|lmao|rofl|kekw|lul|omegalul|pog|poggers|wtf|omg|holy|insane|"
    r"crazy|no way|what the|clutch|lets go|let's go|hype|rip|bruh|haha+|w|l|😂|🤣|🔥|😱)\b"
    r"|[!?]{3,}|😂|🤣|🔥|😱",
    re.IGNORECASE,
)
SPOKEN_EXCITEMENT_RE = re.compile(
    r"\b(?:oh my god|no way|let's go|lets go|what the (?:hell|fuck)|"
    r"holy (?:shit|crap|cow)|that's crazy|that is crazy|insane|unbelievable|"
    r"i can't believe|hilarious|haha+|wow|whoa|let's fucking go)\b",
    re.IGNORECASE,
)


def spoken_excitement(text: str) -> bool:
    return bool(SPOKEN_EXCITEMENT_RE.search(text))


@dataclass
class ChatBucket:
    count: int = 0
    authors: set[str] = field(default_factory=set)
    reactions: int = 0

    def add_irc_line(self, line: str) -> bool:
        match = MESSAGE_RE.search(line)
        if not match:
            return False
        self.add_message(match.group("user"), match.group("text"))
        return True

    def add_message(self, user: str, text: str) -> None:
        self.count += 1
        self.authors.add(user.casefold())
        if REACTION_RE.search(text):
            self.reactions += 1


@dataclass(frozen=True)
class Highlight:
    second: int
    count: int
    unique_chatters: int
    reactions: int
    baseline: float
    reason: str


class HighlightDetector:
    def __init__(self, bucket_seconds: int, min_messages: int, spike_factor: float,
                 cooldown_seconds: int) -> None:
        self.bucket_seconds = bucket_seconds
        self.min_messages = min_messages
        self.spike_factor = spike_factor
        self.cooldown_seconds = cooldown_seconds
        self.history: collections.deque[int] = collections.deque(maxlen=6)
        self.reaction_history: collections.deque[int] = collections.deque(maxlen=6)
        self.bucket = ChatBucket()
        self.last_highlight = -cooldown_seconds

    def add_line(self, line: str) -> bool:
        return self.bucket.add_irc_line(line)

    def add_message(self, user: str, text: str) -> None:
        self.bucket.add_message(user, text)

    def close_bucket(self, bucket_index: int) -> Highlight | None:
        current = self.bucket
        at = bucket_index * self.bucket_seconds + self.bucket_seconds // 2
        baseline = statistics.median(self.history) if len(self.history) >= 3 else 0
        reaction_baseline = (statistics.median(self.reaction_history)
                             if len(self.reaction_history) >= 3 else 0)
        result = None
        unique_min = max(5, self.min_messages // 3)
        if (len(self.history) >= 3 and current.count >= self.min_messages
                and len(current.authors) >= unique_min
                and at - self.last_highlight >= self.cooldown_seconds):
            relative = current.count / max(1, baseline)
            reaction_min = max(3, round(current.count * 0.15))
            if relative >= self.spike_factor * 1.4:
                reason = "strong_chat_spike"
            elif relative >= self.spike_factor and current.reactions >= reaction_min:
                reason = "chat_spike_with_reactions"
            elif (current.reactions >= max(12, round(reaction_baseline * 2.2))
                  and current.reactions >= round(current.count * 0.25)):
                reason = "reaction_burst"
            else:
                reason = ""
            if reason:
                result = Highlight(at, current.count, len(current.authors),
                                   current.reactions, baseline, reason)
                self.last_highlight = at
        self.history.append(current.count)
        self.reaction_history.append(current.reactions)
        self.bucket = ChatBucket()
        return result
