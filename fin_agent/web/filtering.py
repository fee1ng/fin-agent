"""Filter before persistence, including fragments split at any character."""
import re

from fin_agent.agent.redaction import ASSIGNMENT, TOKEN


class TextFilter:
    def __init__(self, redactor):
        self.redactor = redactor
        self.depth, self.tag, self.pending = 0, '', ''
        self.keep = max(32, *(len(s) for s in redactor.secrets), 1)

    def feed(self, value, *, final=False):
        visible = []
        for char in value:
            if self.tag or char == '<':
                self.tag += char
                lowered = self.tag.lower()
                if lowered == '<think>':
                    self.depth += 1
                    self.tag = ''
                elif lowered == '</think>':
                    self.depth = max(0, self.depth - 1)
                    self.tag = ''
                elif not any(tag.startswith(lowered) for tag in ('<think>', '</think>')):
                    if not self.depth:
                        visible.append(self.tag[:-1] if self.tag.endswith('<') else self.tag)
                    self.tag = '<' if self.tag.endswith('<') else ''
            elif not self.depth:
                visible.append(char)
        self.pending += ''.join(visible)
        if final:
            # An unfinished tag is suppressed rather than exposing a partial thought tag.
            result, self.pending = self.redactor.text(self.pending), ''
            self.tag = ''
            return result
        limit = len(self.pending) - self.keep
        if limit <= 0:
            return ''
        # Never release a partial credential token. Chinese punctuation is a safe
        # text boundary as well, so prose does not wait for a newline or ASCII space.
        boundaries = list(re.finditer(r'[\s，。；！？、]', self.pending[:limit]))
        cut = boundaries[-1].end() if boundaries else 0
        for pattern in (ASSIGNMENT, TOKEN):
            for match in pattern.finditer(self.pending):
                if match.start() < cut < match.end():
                    cut = match.start()
        for secret in self.redactor.secrets:
            start = self.pending.find(secret)
            while start >= 0:
                if start < cut < start + len(secret):
                    cut = start
                start = self.pending.find(secret, start + 1)
        if not cut and len(self.pending) > max(8192, self.keep * 2):
            self.pending = ''
            return '[REDACTED]'
        result = self.redactor.text(self.pending[:cut])
        self.pending = self.pending[cut:]
        return result


def visible(value, redactor):
    return TextFilter(redactor).feed(str(value), final=True)
