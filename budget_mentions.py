"""Keep spoken money amounts and their explicit scope; never invent store prices."""
import re
from decimal import Decimal

TOKEN = r'(?:\d[\d,]*(?:\.\d+)?|[일이삼사오육칠팔구십백천만억])'
NUMBER = rf'{TOKEN}(?:\s*{TOKEN})*'
WON_END = r'(?=\s|$|[.,!?]|정도|쯤|까지|까진|안|이내|이상|이하|씩|은|을|이|으로|도|만)'
NUMBER_START = r'(?<![\d,])(?:(?=\d)|(?<![가-힣]))'
RANGE = re.compile(rf'{NUMBER_START}({NUMBER})\s*(?:원\s*)?[~∼〜–-]\s*({NUMBER})\s*원{WON_END}')
MONEY = re.compile(rf'{NUMBER_START}({NUMBER})\s*원{WON_END}')
PERSON = re.compile(r'1\s*인|한\s*사람당|한\s*명당|인당|각자')
GROUP = re.compile(r'전체|총|우리\s*(?:넷|네\s*명)|(?:둘|셋|넷|다섯|여섯)이|(?:두|세|네|\d+)\s*명이')
DIGITS = dict(zip('일이삼사오육칠팔구', range(1, 10)))
UNITS = {'십':10, '백':100, '천':1000, '만':10000, '억':100000000}
CATEGORIES = {'meal': r'밥값|식비|식사|점심|저녁|먹을\s*거|먹는\s*거',
              'drink': r'음료|커피|카페|차값',
              'ticket': r'입장료|표값|티켓|관람료',
              'dessert': r'케이크|디저트|빵값'}
CAP = re.compile(r'까지|까진|이내|이하|넘기지|넘지|최대|상한')
REVISION = re.compile(r'아니다|아니야|아니\s|정정|수정|바꾸|변경|올리|늘리|낮추|줄이|말고|아니라|대신')


def amount(value):
    """Parse explicit Sino-Korean units and mixed digits, not phonetic guesses."""
    compact = re.sub(r'\s+', '', value).replace(',', '')
    parts = re.findall(r'\d+(?:\.\d+)?|[일이삼사오육칠팔구십백천만억]', compact)
    if ''.join(parts) != compact:
        raise ValueError('Invalid money number')
    total = section = Decimal(0)
    pending = None
    small_unit, large_unit = 10000, 10**12
    for part in parts:
        if part not in UNITS:
            if pending is not None:
                raise ValueError('Ambiguous money digits')
            pending = Decimal(DIGITS[part]) if part in DIGITS else Decimal(part)
            continue
        unit = UNITS[part]
        if unit >= 10000:
            if unit >= large_unit:
                raise ValueError('Repeated money unit')
            section += pending if pending is not None else 0
            total += (section or 1) * unit
            section, pending, small_unit, large_unit = Decimal(0), None, 10000, unit
        else:
            if unit >= small_unit:
                raise ValueError('Repeated money unit')
            section += (pending if pending is not None else 1) * unit
            pending, small_unit = None, unit
    return total + section + (pending if pending is not None else 0)


def mentions(clause):
    values, occupied = [], []
    for match in RANGE.finditer(clause):
        occupied.append(match.span())
        try:
            scale = max((unit for word, unit in UNITS.items() if word in match[2]), default=1)
            low = int(amount(match[1]) * (scale if not any(word in match[1] for word in UNITS) else 1))
            high = int(amount(match[2]))
        except ValueError:
            continue
        if 0 < low <= high <= 100000000:
            values.append((match.start(), match.end(), f'{low:,}~{high:,}', 'range'))
    for match in MONEY.finditer(clause):
        if any(start < match.end() and match.start() < end for start, end in occupied):
            continue
        if match.start() and clause[match.start()-1] in '-−':
            continue
        try:
            value = int(amount(match[1]))
        except ValueError:
            continue
        if 0 < value <= 100000000:
            values.append((match.start(), match.end(), f'{value:,}', 'estimate'))
    result = []
    ordered = sorted(values)
    for index, (start, end, value, kind) in enumerate(ordered):
        before = clause[:start]
        next_start = ordered[index + 1][0] if index + 1 < len(ordered) else len(clause)
        after = clause[end:next_start]
        person = list(PERSON.finditer(before))
        group = list(GROUP.finditer(before))
        person_at = person[-1].start() if person else -1
        group_at = group[-1].start() if group else -1
        if re.match(r'\s*씩', after) or person_at > group_at:
            scope, prefix = 'person', '1인 '
        elif group_at >= 0:
            scope, prefix = 'group', '전체 '
        else:
            scope, prefix = 'unspecified', ''
        categories = [(m.start(), category) for category, pattern in CATEGORIES.items()
                      for m in re.finditer(pattern, before)]
        category = max(categories, default=(-1, 'unspecified'))[1]
        # An explicit upper bound supersedes a rough shared estimate. Separate
        # people's hard limits and other expense categories remain separate.
        suffix = re.split(r'[.!?\n]|그리고|하지만', after, maxsplit=1)[0][:24]
        is_cap = bool(CAP.search(suffix) or re.search(r'최대\s*$', before))
        if is_cap: kind = 'cap'
        revision = bool(REVISION.search(before[-50:]) or REVISION.search(suffix))
        rejected = bool(re.match(r'\s*(?:은|는|이|가)?\s*(?:말고|아니라)', after))
        result.append({'requirement': f'{prefix}예산 {value}원 언급', 'scope': scope,
                       'category': category, 'kind': kind, 'revision': revision,
                       'rejected': rejected, 'start': start, 'end': end})
    return result


def requirements(clause):
    return [value['requirement'] for value in mentions(clause) if not value['rejected']]


def update(records, utterance, seat, segment_id):
    """Track current stated budgets without replacing other people's limits."""
    for value in mentions(utterance):
        if value['rejected']:
            continue
        value.update(seat=seat, text=utterance, segment_id=segment_id)
        possible = [old for old in records if (
            value['scope'] == 'group' and old['scope'] == 'group'
            or seat != 'unknown' and old['seat'] == seat and
               (old['scope'] == value['scope'] or old['scope'] == 'unspecified' and value['scope'] == 'person'))]
        if value['category'] == 'unspecified':
            known = {old['category'] for old in possible if old['category'] != 'unspecified'}
            if len(known) == 1 and (value['revision'] or value['kind'] == 'cap'):
                value['category'] = known.pop()
        for old in possible:
            compatible = old['category'] == value['category']
            if not compatible:
                continue
            shared_revision = value['scope'] == 'group' and value['revision']
            own_revision = seat != 'unknown' and old['seat'] == seat and value['revision']
            stronger_bound = value['kind'] == 'cap' and old['kind'] == 'estimate'
            if shared_revision or own_revision or stronger_bound:
                records.remove(old)
        if not any(old['seat'] == seat and old['scope'] == value['scope'] and old['category'] == value['category']
                   and old['requirement'] == value['requirement'] for old in records):
            records.append(value)
    return records
