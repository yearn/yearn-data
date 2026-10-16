"""Pinned release rules that make zero reported gain sufficient for zero fees.

Older releases can assess management fees even without gain and are excluded.
0.3.5 caps total fees at gain; the verified 0.4.x releases return early.
"""
ZERO_GAIN_RELEASES = {
    '0.3.5': '90562544f207753c7bd00e0d4cd2da82680777b7',
    '0.4.2': 'f5a576b8c9ae07130df28abedadcad90e49f93f0',
    '0.4.3': 'b626994c5ca2ed4455053809cdd5b75bd567ccbf',
    '0.4.4': '3362e89807af263d6dea8d4ea7fc1a38070a1cfc',
    '0.4.5': '74364b2c33bd0ee009ece975c157f065b592eeaf',
    '0.4.6': '97ca1b2e4fcf20f4be0ff456dabd020bfeb6697b',
}


def zero_gain_rule(version):
    revision = ZERO_GAIN_RELEASES.get(version)
    return f'yearn-vaults@{revision}:zero-gain' if revision else None
