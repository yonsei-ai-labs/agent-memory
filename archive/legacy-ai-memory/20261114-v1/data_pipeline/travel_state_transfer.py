"""Prepare new request families, before generating any worker outcomes.

These are controlled booking episodes grounded in TravelPlanner validation
requests and catalogs, not official complete-trip benchmark submissions.
"""
import argparse
import ast
from collections import Counter, defaultdict
import csv
import hashlib
import json
import math
from pathlib import Path

REVISION = '8736504ecfc31b7f8b7e40122873c337e83fff7c'
CSV_HASHES = {'train': '2c02c4a31bf08599ea84013565a05d9a1a0a1b0c7613ded0ca932fb964780da1',
              'validation': '0e54b26b13c0b6d50e8683765e930bffca949488c44634e530da899914d40e80'}
PEOPLE = ('Alice', 'Ben', 'Clara', 'Daniel', 'Emma', 'Felix', 'Grace', 'Henry', 'Iris', 'Jacob', 'Lena', 'Mason')
SLOTS = ('lunch', 'dinner', 'accommodation')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def key(value):
    return hashlib.sha256(('travel-state-transfer-v1:' + str(value)).encode()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def catalogs(database):
    result, excluded = {}, []
    for r in csv.DictReader((database / 'restaurants/clean_restaurant_2022.csv').open()):
        id_ = 'R' + r['']
        result[id_] = {'id': id_, 'name': r['Name'], 'city': r['City'], 'kind': 'restaurant',
                       'price': float(r['Average Cost']), 'rating': float(r['Aggregate Rating']),
                       'attributes': sorted(s.strip() for s in r['Cuisines'].split(',') if s.strip()), 'available': True}
    for r in csv.DictReader((database / 'accommodations/clean_accommodations_2022.csv').open()):
        id_ = 'H' + r['']
        try:
            price, rating = float(r['price']), float(r['review rate number'])
            if not math.isfinite(price) or not math.isfinite(rating) or price < 0:
                raise ValueError('Nonfinite catalog value')
        except ValueError:
            excluded.append({'id': id_, 'reason': 'missing_or_invalid_price_or_rating'})
            continue
        result[id_] = {'id': id_, 'name': r['NAME'], 'city': r['city'], 'kind': 'hotel',
                       'price': price, 'rating': rating,
                       'attributes': [s.strip() for s in (r['room type'], r['house_rules']) if s.strip()], 'available': True}
    return result, excluded


def build(source, database, output):
    if output.exists():
        raise FileExistsError(output)
    for name, digest in CSV_HASHES.items():
        if sha(source / (name + '.csv')) != digest:
            raise ValueError('Changed upstream ' + name)
    old = list(csv.DictReader((source / 'train.csv').open()))
    rows = list(csv.DictReader((source / 'validation.csv').open()))
    old_queries = {r['query'].strip() for r in old}
    identity = lambda r: (r['org'], r['dest'], int(r['days']))
    old_families = {identity(r) for r in old}
    catalog, excluded_catalog = catalogs(database)
    by_city = defaultdict(lambda: defaultdict(list))
    for item in catalog.values():
        by_city[item['city']][item['kind']].append(item['id'])
    candidates, exclusions, seen = [], [], set()
    for index, row in enumerate(rows):
        identity_ = identity(row)
        reason = None
        if row['query'].strip() in old_queries or identity_ in old_families:
            reason = 'overlaps_prior_request_or_route_duration'
        elif identity_ in seen:
            reason = 'same_origin_destination_duration_family'
        seen.add(identity_)
        contexts = ast.literal_eval(row['reference_information'])
        cities = sorted({x['Description'].removeprefix('Restaurants in ') for x in contexts
                         if x['Description'].startswith('Restaurants in ')})
        eligible = [city for city in cities if len(by_city[city]['restaurant']) >= 12 and len(by_city[city]['hotel']) >= 8]
        if not eligible:
            reason = reason or 'insufficient_grounded_catalog'
        if reason:
            exclusions.append({'upstream_row': index, 'reason': reason})
            continue
        group = key(identity_)
        city = min(eligible, key=lambda c: key(group + c))
        menus = {kind: sorted(by_city[city][kind], key=lambda id_: key(group + id_))[:n]
                 for kind, n in [('restaurant', 12), ('hotel', 8)]}
        candidates.append({'family_id': group, 'upstream_row': index, 'origin': row['org'], 'destination': row['dest'],
                           'days': int(row['days']), 'level': row['level'], 'query': row['query'], 'city': city,
                           'dates': ast.literal_eval(row['date']), 'menus': menus})
    # Keep each route/duration together. Stratify duration and cycle difficulty.
    selected = []
    for days in (3, 5, 7):
        levels = {level: sorted([r for r in candidates if r['days'] == days and r['level'] == level], key=lambda r: r['family_id'])
                  for level in ('easy', 'medium', 'hard')}
        ordered = []
        while any(levels.values()):
            for level in levels:
                if levels[level]:
                    ordered.append(levels[level].pop(0))
        if len(ordered) < 40:
            raise ValueError('Not enough independently grouped requests for ' + str(days))
        dev_n = 6 if days == 7 else 7
        for i, row in enumerate(ordered[:40]):
            split = 'train' if i < 20 else ('dev' if i < 20 + dev_n else 'test')
            selected.append(dict(row, split=split, pilot=(split == 'train' and i < 4)))
    episodes, jobs = [], []
    for row in selected:
        group, days = row['family_id'], row['days']
        names = sorted(PEOPLE, key=lambda p: key(group + p))[:3]
        memory = []
        for person_index, person in enumerate(names):
            tasks = []
            for day in range(1, days + 1):
                for slot in SLOTS:
                    kind = 'hotel' if slot == 'accommodation' else 'restaurant'
                    options = row['menus'][kind]
                    index = int(key(group + person + str(day) + slot)[:8], 16)
                    anchor = catalog[options[index % len(options)]]
                    goal = {'attribute': anchor['attributes'][index % len(anchor['attributes'])],
                            'min_rating': max(0., anchor['rating'] - 1.),
                            'max_price': anchor['price'] + (20 if kind == 'restaurant' else 50)}
                    meta = {'memory_id': f'{group[:12]}-p{person_index}-d{day}-{slot}', 'person': person,
                            'day': day, 'slot': slot, 'prior_round': person_index + 1}
                    task = dict(meta, menu_ids=options, goal=goal)
                    tasks.append(task)
                    memory.append(meta)
            jobs.append({'job_id': group + '--past-' + str(person_index), 'family_id': group, 'split': row['split'],
                         'pilot': row['pilot'], 'person': person, 'city': row['city'], 'tasks': tasks})
        for case_index in range(2):
            a = int(key(group + str(case_index))[:8], 16)
            refs = [next(m for m in memory if m['person'] == names[case_index] and m['day'] == a % days + 1 and m['slot'] == 'dinner'),
                    next(m for m in memory if m['person'] == names[(case_index + 1) % 3] and m['day'] == (a // days) % days + 1 and m['slot'] == 'accommodation')]
            mode = 'reuse' if case_index == 0 else 'comparison'
            dinner = f"{refs[0]['person']}'s dinner on day {refs[0]['day']}"
            hotel = f"{refs[1]['person']}'s accommodation on day {refs[1]['day']}"
            if mode == 'reuse':
                question = (f"For my dinner, choose the same restaurant as {dinner}. For my accommodation, choose the same hotel as {hotel}. "
                            "Each chosen place must currently be available. Return NONE for a component if its required place is unavailable.")
            else:
                question = (f"For my dinner, choose a currently available restaurant costing at least 5 less than the recorded past price of {dinner}, with a rating at least 3. "
                            f"For my accommodation, choose a currently available hotel costing no more than the recorded past price of {hotel}, with a rating at least 3. "
                            "Any choice satisfying every condition is acceptable. Return NONE for a component if no listed choice satisfies its conditions.")
            episodes.append({'case_id': group[:16] + '--case-' + str(case_index), 'family_id': group,
                             'split': row['split'], 'pilot': row['pilot'], 'city': row['city'], 'days': days,
                             'question': question, 'mode': mode, 'memories': memory, 'menus': row['menus'],
                             'reference_keys': [{k: m[k] for k in ('person', 'day', 'slot')} for m in refs]})
    used = {id_ for r in selected for values in r['menus'].values() for id_ in values}
    output.mkdir(parents=True)
    write(output / 'families.json', selected)
    write(output / 'cases.json', episodes)
    write(output / 'source-jobs.json', jobs)
    write(output / 'catalog.json', {id_: catalog[id_] for id_ in sorted(used)})
    write(output / 'source-audit.json', {'revision': REVISION, 'input_sha256': {n: sha(source / (n + '.csv')) for n in CSV_HASHES},
          'catalog_sha256': {str(p.relative_to(database)): sha(p) for p in [database / 'restaurants/clean_restaurant_2022.csv', database / 'accommodations/clean_accommodations_2022.csv']},
          'upstream_validation_rows': len(rows), 'eligible_families': len(candidates), 'selected_families': len(selected),
          'splits': dict(Counter(r['split'] for r in selected)), 'pilot_train_families': sum(r['pilot'] for r in selected),
          'excluded_catalog_rows': excluded_catalog,
          'exclusions': exclusions, 'unselected_families': [r['family_id'] for r in candidates if r['family_id'] not in {s['family_id'] for s in selected}],
          'same_domain_catalog_transfer': True, 'independent_external_domain_test': False,
          'limits': ['Synthetic follow-up requests and availability interventions, grounded catalog values', 'Original trip requirements are provenance, not the complete task being scored', 'Route/duration grouped; shared cities and catalog entities remain']})
    write(output / 'manifest.json', {'sha256': {p.name: sha(p) for p in sorted(output.iterdir())}, 'generator_sha256': sha(__file__)})
    print(json.dumps({'families': len(selected), 'pilot_families': sum(r['pilot'] for r in selected), 'past_jobs': len(jobs), 'cases': len(episodes)}, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    for name in ('source', 'database', 'output'):
        p.add_argument('--' + name, type=Path, required=True)
    a = p.parse_args()
    build(a.source, a.database, a.output)
