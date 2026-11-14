"""Build provide/withhold contrasts from knowledge-update questions of public memory benchmarks."""
import argparse
import collections
import hashlib
import json
import math
from pathlib import Path
import re

from training.memory_selector.external_updates_protocol import BASE_CONFIG, EXTERNAL_SYSTEM

SCHEMA = 'external-memory-updates-v1'
BEAM_REPOSITORY = 'https://github.com/mohammadtavakoli78/BEAM'
LONGMEMEVAL_DATASET = 'https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned'
MAX_RECORD_TOKENS = 512
MAX_ANSWER_WORDS = 5
DISTRACTORS = 6
SPLIT_CYCLE = ('confirm', 'confirm', 'train', 'dev')
DATA_FILES = ('cases.jsonl', 'annotations.jsonl', 'decisions.jsonl', 'jobs.jsonl', 'selector-inputs.jsonl',
              'selector-labels.jsonl', 'exclusions.jsonl', 'protocol.json', 'source-provenance.json')
INCLUSION = [
    'BEAM: knowledge_update questions of the listed sizes with separate, non-overlapping original_info and updated_info message ids',
    'BEAM: exactly one rubric item of the form "LLM response should state: X" with X of at most five words',
    'LongMemEval-S: answerable knowledge-update questions whose two evidence sessions both have a marked turn',
    'LongMemEval-S: every answer alternative has at most five words, so it can be recognised by its value',
    'the new evidence states an accepted answer value and the old evidence states none',
    'every old message precedes every new message',
    'no evidence message is longer than max_record_tokens',
    'distractors: best lexical matches among other user messages (BEAM: same conversation; LongMemEval-S: other sessions) that state neither an accepted nor the superseded value',
]
LABEL_NOTE = ('Roles and "needed" are read from the source annotations. They are not measured effects of supplying '
              'a record to a worker.')
_RUBRIC = re.compile(r'LLM response should (?:state|mention|contain|include)[: ]+(.*)')
_NUMBERS = re.compile(r'(?<![\d.:/])\d+(?:\.\d+)?(?![\d:/]|\.\d)')
_NUMBER_WORDS = {w: str(i) for i, w in enumerate(
    'zero one two three four five six seven eight nine ten eleven twelve'.split())}
_NUMBER_WORDS.update(once='1', twice='2', thrice='3')
_MARKER = re.compile(r'\s*->->\s*\d+\s*,\s*\d+\s*$')
_NUMBER_ANSWER = re.compile(r'\$?(\d+(?:\.\d+)?)(?: %)?(?: [a-z][a-z ]*)?')
_MONTHS = frozenset('january february march april may june july august september october november december'.split())
_YEAR = re.compile(r'(?:19|20)\d\d')


def normalize(text):
    """Comparable form: lower case, 1,350 -> 1350, 27th -> 27, 250ms -> 250 ms, small number words as digits."""
    s = str(text).lower()
    s = re.sub(r'(?<=\d),(?=\d{3}(?!\d))', '', s)
    s = re.sub(r'(?<=\d)(?:st|nd|rd|th)\b', '', s)
    s = re.sub(r'(?<=\d)(?=[a-z%])', ' ', s)
    s = re.sub(r'(?<!\d)[.:/]|[.:/](?!\d)', ' ', s)
    s = re.sub(r'[^a-z0-9$%.:/ ]+', ' ', s)
    return ' '.join(_NUMBER_WORDS.get(w, w) for w in s.split())


def answer_key(value):
    """How an expected value is recognised in a text.

    number: one number, optionally followed by unit words ('165 commits') -> its digits
    tokens: a phrase holding numbers, clock times, ratios or month names ('April 5, 2024', 'around 140MB')
            -> every such token; a year next to a month is not required
    text:   anything else ('Paris', 'the suburbs') -> the whole phrase without a leading article
    """
    text = normalize(value)
    match = _NUMBER_ANSWER.fullmatch(text)
    if match:
        return {'kind': 'number', 'value': match.group(1)}
    tokens = [w.lstrip('$') for w in text.split() if any(c.isdigit() for c in w) or w in _MONTHS]
    if any(t in _MONTHS for t in tokens):
        tokens = [t for t in tokens if not _YEAR.fullmatch(t)]
    if tokens:
        return {'kind': 'tokens', 'values': list(dict.fromkeys(tokens))}
    return {'kind': 'text', 'value': re.sub(r'^(?:a|an|the) ', '', text)}


def _has_token(haystack, token):
    """A plain number must not be part of a longer number, time or ratio; other tokens must stand alone."""
    if re.fullmatch(r'\d+(?:\.\d+)?', token):
        return re.search(r'(?<![\d.:/])' + re.escape(token) + r'(?![\d:/]|\.\d)', haystack) is not None
    return f' {token} ' in f' {haystack} '


def contains(text, key):
    haystack = normalize(text)
    if key['kind'] == 'number':
        return _has_token(haystack, key['value'])
    if key['kind'] == 'tokens':
        return all(_has_token(haystack, token) for token in key['values'])
    return f" {key['value']} " in f' {haystack} '


def strip_marker(text):
    """BEAM user messages end with a plan index such as ' ->-> 2,14'."""
    return _MARKER.sub('', text)


def _words(text):
    return re.findall(r'[a-z0-9]+', str(text).lower())


def lexical_order(query, records, k1=1.5, b=0.75):
    """Record ids by BM25 score for the query, best first; ties by record id."""
    docs = {r['record_id']: _words(r['text']) for r in records}
    if not docs:
        return []
    average = sum(len(words) for words in docs.values()) / len(docs) or 1.0
    frequency = collections.Counter(w for words in docs.values() for w in set(words))
    query_words = sorted(set(_words(query)))        # fixed order, so the float sum never depends on the hash seed

    def score(words):
        counts = collections.Counter(words)
        total = 0.0
        for w in query_words:
            if counts[w]:
                idf = math.log(1 + (len(docs) - frequency[w] + 0.5) / (frequency[w] + 0.5))
                total += idf * counts[w] * (k1 + 1) / (counts[w] + k1 * (1 - b + b * len(words) / average))
        return total

    return sorted(docs, key=lambda rid: (-score(docs[rid]), rid))


def _numbers(text):
    return set(_NUMBERS.findall(normalize(text)))


def _raw(source, family_id, case_id, split, question, question_time, stale_hint):
    return {'source': source, 'family_id': family_id, 'case_id': case_id, 'split': split, 'question': question,
            'question_time': question_time, 'accept_values': [], 'stale_hint': stale_hint,
            'old': [], 'new': [], 'pool': [], 'exclusion': None}


def beam_raw_cases(root, sizes=('100K', '500K', '1M')):
    """One raw case per BEAM knowledge-update question; `exclusion` says why a question cannot be used."""
    root = Path(root)
    for size in sizes:
        folders = [p for p in (root / 'chats' / size).iterdir() if p.is_dir() and p.name.isdigit()]
        for folder in sorted(folders, key=lambda p: int(p.name)):
            family_id = f'beam:{size}-{folder.name}'
            messages = _beam_messages(json.loads((folder / 'chat.json').read_text()), family_id)
            questions = json.loads((folder / 'probing_questions' / 'probing_questions.json').read_text())
            for index, question in enumerate(questions.get('knowledge_update', []), 1):
                yield _beam_case(question, f'{family_id}:ku{index}', family_id, messages)


def _beam_messages(chat, family_id):
    """Messages by id. A message carries the latest time anchor stated at or before it."""
    messages, anchor = {}, None
    for batch in chat:
        for turn in batch['turns']:
            for m in turn:
                anchor = m.get('time_anchor') or anchor
                messages[m['id']] = {'record_id': f"{family_id}:m{m['id']}", 'order': m['id'], 'time': anchor,
                                     'speaker': m['role'], 'text': strip_marker(m['content'])}
    return messages


def _beam_case(question, case_id, family_id, messages):
    raw = _raw('beam', family_id, case_id, None, question['question'], None, question.get('potential_confusion'))
    ids = question.get('source_chat_ids')
    rubric = [_RUBRIC.fullmatch(item) for item in question.get('rubric') or []]
    if not isinstance(ids, dict):
        raw['exclusion'] = 'no_evidence_ids'
        return raw
    old, new = ids.get('original_info') or [], ids.get('updated_info') or []
    if not old or not new or set(old) & set(new):
        raw['exclusion'] = 'old_or_new_ids_missing_or_overlapping'
    elif any(i not in messages for i in old + new):
        raw['exclusion'] = 'evidence_id_not_in_chat'
    elif len(rubric) != 1 or rubric[0] is None or len(rubric[0].group(1).split()) > 5:
        raw['exclusion'] = 'rubric_not_one_short_value'
    else:
        evidence = set(old) | set(new)
        raw['accept_values'] = [rubric[0].group(1).strip()]
        raw['old'] = [messages[i] for i in sorted(old)]
        raw['new'] = [messages[i] for i in sorted(new)]
        raw['pool'] = [m for i, m in sorted(messages.items()) if m['speaker'] == 'user' and i not in evidence]
    return raw


def longmemeval_raw_cases(path):
    """One raw case per answerable LongMemEval knowledge-update question."""
    for row in json.loads(Path(path).read_text()):
        question_id = str(row['question_id'])
        if row['question_type'] == 'knowledge-update' and not question_id.endswith('_abs'):
            yield _longmemeval_case(row, f'lme:{question_id}')


def _longmemeval_case(row, family_id):
    raw = _raw('longmemeval_s', family_id, family_id, 'design', row['question'], row['question_date'], None)
    raw['accept_values'] = _answer_alternatives(row['answer'])
    gold = set(row['answer_session_ids'])
    sessions = sorted(zip(row['haystack_dates'], range(len(row['haystack_sessions'])),
                          row['haystack_session_ids'], row['haystack_sessions']), key=lambda s: s[:2])
    evidence, order = [], 0
    for date, session_index, session_id, turns in sessions:
        marked = []
        for turn_index, turn in enumerate(turns):
            record = {'record_id': f'{family_id}:s{session_index}t{turn_index}', 'order': order, 'time': date,
                      'speaker': turn['role'], 'text': turn['content']}
            order += 1
            if session_id in gold:
                if turn.get('has_answer'):
                    marked.append(record)
            elif turn['role'] == 'user':
                raw['pool'].append(record)
        if session_id in gold:
            evidence.append(marked)
    if len(evidence) != 2:
        raw['exclusion'] = 'not_two_evidence_sessions'
    elif not evidence[0] or not evidence[1]:
        raw['exclusion'] = 'evidence_turn_not_marked'
    elif any(len(a.split()) > MAX_ANSWER_WORDS for a in raw['accept_values']):
        raw['exclusion'] = 'answer_too_long_for_value_matching'
    else:
        raw['old'], raw['new'] = evidence
    return raw


def _answer_alternatives(answer):
    """'25 minutes and 50 seconds (or 25:50)' -> ['25 minutes and 50 seconds', '25:50']."""
    text = str(answer)
    extra = re.findall(r'\(or ([^)]*)\)', text)
    main = re.sub(r'\s*\(or [^)]*\)', '', text)
    return [a.strip() for a in [main] + extra if a.strip()]


def _stale_value(raw, accept, old_text, new_text):
    """The superseded number named in the source's note, kept only if the old evidence states it and the new does not.

    Without a note no value is guessed: a number found only in the old evidence need not be the old answer.
    """
    current = {k['value'] for k in accept if k['kind'] == 'number'}
    if not raw['stale_hint'] or len(current) != len(accept):
        return None, None
    as_key = lambda n: {'kind': 'number', 'value': n}
    confirmed = sorted(n for n in _numbers(raw['stale_hint']) - current
                       if contains(old_text, as_key(n)) and not contains(new_text, as_key(n)))
    return (as_key(confirmed[0]), 'source-confusion-note') if len(confirmed) == 1 else (None, None)


def build_case(raw, count_tokens, max_record_tokens=MAX_RECORD_TOKENS, distractors=DISTRACTORS):
    """Return (case, annotation, None), or (None, None, reason) when the question cannot be used.

    The case holds only what a selector or worker may see. Roles and answers go to the annotation.
    """
    if raw['exclusion']:
        return None, None, raw['exclusion']
    accept = [answer_key(v) for v in raw['accept_values']]
    old_text = ' '.join(m['text'] for m in raw['old'])
    new_text = ' '.join(m['text'] for m in raw['new'])
    if not any(contains(new_text, k) for k in accept):
        return None, None, 'answer_not_in_new_evidence'
    if any(contains(old_text, k) for k in accept):
        return None, None, 'answer_also_in_old_evidence'
    if max(m['order'] for m in raw['old']) > min(m['order'] for m in raw['new']):
        return None, None, 'old_not_before_new'
    sized = lambda m: dict(m, tokens=count_tokens(m['text']))
    evidence = [sized(m) for m in raw['old'] + raw['new']]
    if any(m['tokens'] > max_record_tokens for m in evidence):
        return None, None, 'evidence_record_too_long'
    stale, stale_source = _stale_value(raw, accept, old_text, new_text)

    blocked = accept + ([stale] if stale else [])
    pool = {m['record_id']: m for m in raw['pool']}
    chosen = []
    for record_id in lexical_order(raw['question'], raw['pool']):
        if len(chosen) == distractors:
            break
        if any(contains(pool[record_id]['text'], k) for k in blocked):
            continue
        candidate = sized(pool[record_id])
        if candidate['tokens'] <= max_record_tokens:
            chosen.append(candidate)

    records = sorted(evidence + chosen, key=lambda m: m['order'])
    case = {k: raw[k] for k in ('case_id', 'source', 'family_id', 'split', 'question', 'question_time')}
    case['records'] = [{k: m[k] for k in ('record_id', 'order', 'time', 'speaker', 'text', 'tokens')} for m in records]
    annotation = {
        'case_id': raw['case_id'],
        'new_record_ids': [m['record_id'] for m in raw['new']],
        'old_record_ids': [m['record_id'] for m in raw['old']],
        'distractor_record_ids': [m['record_id'] for m in sorted(chosen, key=lambda m: m['order'])],
        'accept': accept, 'stale': stale, 'stale_source': stale_source,
        'lexical_order': lexical_order(raw['question'], case['records']),
    }
    return case, annotation, None


CONDITIONS = ('none', 'new_only', 'old_only', 'both', 'current_new', 'current_new_plus_old',
              'lexical_top2', 'latest_of_top2')


def condition_bundles(case, annotation):
    """Records each condition supplies, as past records ('memory') or as the ongoing conversation ('current').

    The first six use the evidence labels. The last two are simple policies that see only the lexical ranking.
    """
    order = {r['record_id']: r['order'] for r in case['records']}
    chronological = lambda ids: sorted(ids, key=lambda i: order[i])
    new, old = chronological(annotation['new_record_ids']), chronological(annotation['old_record_ids'])
    top2 = chronological(annotation['lexical_order'][:2])
    bundles = {
        'none': ([], []), 'new_only': ([], new), 'old_only': ([], old), 'both': ([], chronological(old + new)),
        'current_new': (new, []), 'current_new_plus_old': (new, old),
        'lexical_top2': ([], top2), 'latest_of_top2': ([], top2[-1:]),
    }
    return {name: {'current': current, 'memory': memory} for name, (current, memory) in bundles.items()}


def render_messages(case, current_ids=(), memory_ids=()):
    """Worker prompt. Records appear oldest first and carry no ids or role labels."""
    by_id = {r['record_id']: r for r in case['records']}
    pick = lambda ids: sorted((by_id[i] for i in ids), key=lambda r: r['order'])
    lines = [f"Question date: {case['question_time']}"] if case['question_time'] else []
    lines.append('Current conversation:')
    lines += [f"{r['speaker']}: {r['text']}" for r in pick(current_ids)] or ['(no earlier messages)']
    lines.append('Past conversation records supplied, oldest first:')
    lines += [f"[{n}] {r['time'] or 'date unknown'}, {r['speaker']}: {r['text']}"
              for n, r in enumerate(pick(memory_ids), 1)] or ['(none)']
    lines.append(f"Question: {case['question']}")
    return [{'role': 'system', 'content': EXTERNAL_SYSTEM}, {'role': 'user', 'content': '\n'.join(lines)}]


def signature(messages):
    """Same message signature as the existing worker protocols."""
    return hashlib.sha256(json.dumps(messages, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def decisions(case, annotation):
    """One decision row per condition and the distinct worker jobs they point to."""
    rows, jobs = [], {}
    bundles = condition_bundles(case, annotation)
    for name in CONDITIONS:
        messages = render_messages(case, bundles[name]['current'], bundles[name]['memory'])
        job_id = signature(messages)
        jobs[job_id] = {'job_id': job_id, 'messages': messages}
        rows.append({'case_id': case['case_id'], 'condition': name, 'current_record_ids': bundles[name]['current'],
                     'memory_record_ids': bundles[name]['memory'], 'job_id': job_id})
    return rows, jobs


def selector_rows(case, annotation):
    """One (question, candidate record) row per record. Inputs and labels are returned separately."""
    rank = {record_id: n for n, record_id in enumerate(annotation['lexical_order'], 1)}
    role = {record_id: name for name in ('old', 'new', 'distractor') for record_id in annotation[f'{name}_record_ids']}
    inputs, labels = [], []
    for r in case['records']:
        example_id = f"{case['case_id']}|{r['record_id']}"
        inputs.append({'example_id': example_id, 'case_id': case['case_id'], 'record_id': r['record_id'],
                       'source': case['source'], 'family_id': case['family_id'], 'split': case['split'],
                       'question': case['question'], 'question_time': case['question_time'],
                       'record_time': r['time'], 'record_speaker': r['speaker'], 'record_text': r['text'],
                       'record_tokens': r['tokens'], 'lexical_rank': rank[r['record_id']]})
        labels.append({'example_id': example_id, 'role': role[r['record_id']],
                       'needed': int(role[r['record_id']] == 'new')})
    return inputs, labels


def _file_sha(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _readl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def _write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def _write_rows(path, rows):
    Path(path).write_text(''.join(json.dumps(r, ensure_ascii=False, allow_nan=False) + '\n' for r in rows))


def _beam_source(root, sizes):
    root = Path(root)
    files = sorted(p for size in sizes for pattern in ('*/chat.json', '*/probing_questions/probing_questions.json')
                   for p in (root / 'chats' / size).glob(pattern))
    entries = [[p.relative_to(root).as_posix(), _file_sha(p)] for p in files]
    return {'files': len(entries), 'sha256': hashlib.sha256(json.dumps(entries).encode()).hexdigest()}


def prepare(beam_root, longmemeval_path, output, count_tokens, beam_sizes=('100K', '500K', '1M'), provenance=None):
    """Write the dataset folder and return its summary. `count_tokens` maps a text to its token count."""
    output = Path(output)
    if output.exists():
        raise FileExistsError(output)
    raws = list(beam_raw_cases(beam_root, beam_sizes))
    splits = assign_splits(r['family_id'] for r in raws)
    for raw in raws:
        raw['split'] = splits[raw['family_id']]
    raws += list(longmemeval_raw_cases(longmemeval_path))

    cases, annotations, exclusions, rows, jobs, selector_inputs, selector_labels = [], [], [], [], {}, [], []
    for raw in raws:
        case, annotation, reason = build_case(raw, count_tokens)
        if reason:
            exclusions.append({'case_id': raw['case_id'], 'source': raw['source'], 'family_id': raw['family_id'],
                               'reason': reason})
            continue
        cases.append(case)
        annotations.append(annotation)
        case_rows, case_jobs = decisions(case, annotation)
        rows += case_rows
        jobs.update(case_jobs)
        inputs, labels = selector_rows(case, annotation)
        selector_inputs += inputs
        selector_labels += labels

    group = lambda case: f"{case['source']}/{case['split']}"
    stale = {a['case_id'] for a in annotations if a['stale']}
    summary = {
        'questions': dict(sorted(collections.Counter(r['source'] for r in raws).items())),
        'cases': dict(sorted(collections.Counter(group(c) for c in cases).items())),
        'families': {g: len({c['family_id'] for c in cases if group(c) == g}) for g in sorted({group(c) for c in cases})},
        'cases_with_stale_value': dict(sorted(collections.Counter(group(c) for c in cases if c['case_id'] in stale).items())),
        'excluded': dict(sorted(collections.Counter(e['reason'] for e in exclusions).items())),
        'decisions': len(rows), 'distinct_jobs': len(jobs),
    }
    provenance = dict(provenance or {})
    sources = {
        'beam': {**provenance.pop('beam', {}), **_beam_source(beam_root, beam_sizes), 'sizes': list(beam_sizes)},
        'longmemeval_s': {**provenance.pop('longmemeval_s', {}), 'sha256': _file_sha(longmemeval_path)},
        **provenance,
    }
    protocol = {
        'schema': SCHEMA, 'max_record_tokens': MAX_RECORD_TOKENS, 'distractors': DISTRACTORS,
        'split_cycle': list(SPLIT_CYCLE), 'conditions': list(CONDITIONS),
        'system_prompt_sha256': hashlib.sha256(EXTERNAL_SYSTEM.encode()).hexdigest(),
        'inclusion': INCLUSION, 'labels': LABEL_NOTE,
    }

    output.mkdir(parents=True)
    _write_rows(output / 'cases.jsonl', cases)
    _write_rows(output / 'annotations.jsonl', annotations)
    _write_rows(output / 'decisions.jsonl', rows)
    _write_rows(output / 'jobs.jsonl', [jobs[k] for k in sorted(jobs)])
    _write_rows(output / 'selector-inputs.jsonl', selector_inputs)
    _write_rows(output / 'selector-labels.jsonl', selector_labels)
    _write_rows(output / 'exclusions.jsonl', exclusions)
    _write(output / 'protocol.json', protocol)
    _write(output / 'source-provenance.json', sources)
    _write(output / 'manifest.json', {'schema': SCHEMA, 'sha256': {n: _file_sha(output / n) for n in DATA_FILES},
                                      'summary': summary})
    return summary


def export_inputs(data, output, worker_provenance, sources=None, splits=None, conditions=None):
    """Write the worker upload folder (messages only) for the chosen sources, splits and conditions."""
    data, output = Path(data), Path(output)
    if output.exists():
        raise FileExistsError(output)
    manifest = json.loads((data / 'manifest.json').read_text())
    for name, expected in manifest['sha256'].items():
        if _file_sha(data / name) != expected:
            raise ValueError('Changed dataset file: ' + name)
    cases = {c['case_id']: c for c in _readl(data / 'cases.jsonl')}
    chosen = [d for d in _readl(data / 'decisions.jsonl')
              if (sources is None or cases[d['case_id']]['source'] in sources)
              and (splits is None or cases[d['case_id']]['split'] in splits)
              and (conditions is None or d['condition'] in conditions)]
    wanted = {d['job_id'] for d in chosen}
    jobs = [j for j in _readl(data / 'jobs.jsonl') if j['job_id'] in wanted]
    if not jobs:
        raise ValueError('No worker jobs selected')
    output.mkdir(parents=True)
    _write_rows(output / 'inputs.jsonl', jobs)
    _write(output / 'config.json', {**BASE_CONFIG, 'main_jobs': len(jobs)})
    _write(output / 'worker-provenance.json', worker_provenance)
    uploaded = ('inputs.jsonl', 'config.json', 'worker-provenance.json')
    _write(output / 'input-manifest.json', {'sha256': {n: _file_sha(output / n) for n in uploaded}})
    selection = {'dataset_manifest_sha256': _file_sha(data / 'manifest.json'),
                 'sources': sorted(sources) if sources else None, 'splits': sorted(splits) if splits else None,
                 'conditions': sorted(conditions) if conditions else None,
                 'cases': len({d['case_id'] for d in chosen}), 'decisions': len(chosen)}
    _write(output / 'selection.json', selection)
    return {'worker_jobs': len(jobs), 'decisions': len(chosen), 'cases': selection['cases'],
            'repeat_jobs': BASE_CONFIG['repeat_jobs'], 'labels_uploaded': False}


def assign_splits(family_ids):
    """Conversation-level split, fixed by a hash order inside each size group ('beam:500K-7' -> group 'beam:500K')."""
    groups = collections.defaultdict(list)
    for family_id in set(family_ids):
        groups[family_id.rsplit('-', 1)[0]].append(family_id)
    splits = {}
    for members in groups.values():
        ordered = sorted(members, key=lambda f: hashlib.sha256(f.encode()).hexdigest())
        for index, family_id in enumerate(ordered):
            splits[family_id] = SPLIT_CYCLE[index % len(SPLIT_CYCLE)]
    return splits


def token_counter(tokenizer_path):
    """Token counts from a tokenizers JSON file; use the worker's tokenizer."""
    from tokenizers import Tokenizer
    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    return lambda text: len(tokenizer.encode(text, add_special_tokens=False).ids)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('prepare', help='build the dataset folder from the two sources')
    p.add_argument('--beam', type=Path, required=True, help='checkout of the BEAM repository')
    p.add_argument('--longmemeval', type=Path, required=True, help='longmemeval_s_cleaned.json')
    p.add_argument('--tokenizer', type=Path, required=True, help='tokenizer.json of the worker model')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--beam-sizes', nargs='+', default=['100K', '500K', '1M'])
    p.add_argument('--beam-revision')
    p.add_argument('--longmemeval-revision')
    e = commands.add_parser('export', help='write the worker upload folder (messages only)')
    e.add_argument('--data', type=Path, required=True)
    e.add_argument('--output', type=Path, required=True)
    e.add_argument('--worker-provenance', type=Path, required=True,
                   help='JSON with the frozen worker record, at top level or under "worker"')
    e.add_argument('--source', nargs='+')
    e.add_argument('--split', nargs='+')
    e.add_argument('--condition', nargs='+', choices=CONDITIONS)
    a = parser.parse_args(argv)
    if a.command == 'prepare':
        provenance = {
            'beam': {'repository': BEAM_REPOSITORY, 'revision': a.beam_revision, 'license': 'CC BY-SA 4.0 (data)'},
            'longmemeval_s': {'dataset': LONGMEMEVAL_DATASET, 'revision': a.longmemeval_revision, 'license': 'MIT'},
            'tokenizer': {'sha256': _file_sha(a.tokenizer)},
        }
        result = prepare(a.beam, a.longmemeval, a.output, token_counter(a.tokenizer), tuple(a.beam_sizes), provenance)
    else:
        record = json.loads(a.worker_provenance.read_text())
        result = export_inputs(a.data, a.output, record.get('worker', record), a.source, a.split, a.condition)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
