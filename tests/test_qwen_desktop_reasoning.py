import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.laptop.bridge import qwen_desktop_mode
cases = [(None, 'auto'), ('', 'auto'), ('low', 'fast'), ('medium', 'thinking'), ('high', 'thinking'), ('xhigh', 'thinking'), ('max', 'thinking'), ('ultra', 'thinking')]
for value, expected in cases:
    actual = qwen_desktop_mode(value)
    assert actual == expected, (value, actual, expected)
try:
    qwen_desktop_mode('bogus')
except ValueError:
    pass
else:
    raise AssertionError('bogus level accepted')
print('qwen desktop mapping: ok', len(cases), 'cases')
