"""Source-grounded startup pilot; all states are diagnostic reconstructions.

Old source and target versions already informed design. This is ONE connected
source family, not new independent SWE tasks or a selector-training dataset.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SOURCE_RUN = ROOT / 'runs/20261107T043844Z-c1316dac'
SOURCE_CARD = ROOT / 'experiments/swe_memory/memory/process-restoration-20261107/django__django-11019-S.json'
IMAGES = {v: 'ai-memory-process07/sweb.eval.x86_64.django_1776_'+v+':latest' for v in ('django-14534','django-16041')}
MODES = ('cold','environment_only','configured_not_ready','ready')
PYTHON = '/opt/miniconda3/envs/testbed/bin/python'
BASE_PRELUDE = """import os, sys
os.chdir('/testbed')
sys.path.insert(0, '/testbed')
os.environ.pop('DJANGO_SETTINGS_MODULE', None)
import django
from django.conf import settings
from django.apps import apps
"""
CUSTOM = """settings.configure(
    SECRET_KEY='pilot-local-fixture', INSTALLED_APPS=[], USE_I18N=False,
    DEBUG=True, MEMORY_PILOT_MARKER='existing-configuration-must-survive',
    TEMPLATES=[{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'APP_DIRS': True}])
"""
PROBE_DESCRIPTION = """After your startup code, the evaluator will use the SAME process to:
1. Check that Django settings are configured and the app registry is ready.
2. Call list(django.apps.apps.get_models()).
3. Instantiate a simple django.forms.Form with a CharField and render that field.
If settings are not configured yet, use this checkout's tests/test_sqlite.py
settings module (import name test_sqlite); do not invent replacement settings.
If settings are already configured, preserve that configuration, including
DEBUG and MEMORY_PILOT_MARKER, and do not replace it with test_sqlite.
Do not modify the Django checkout, install packages, launch another process,
or change/replace the subsequent probe. Your code runs in the existing process.
"""


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def write(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def source_units():
    raw=[json.loads(line) for line in (SOURCE_RUN/'commands.jsonl').read_text().splitlines()]
    agent_commands=[r for r in raw if r['phase']=='agent']
    record=agent_commands[17];command=record['action']['command']
    assert 'PYTHONPATH=tests:. DJANGO_SETTINGS_MODULE=test_sqlite' in command
    assert 'import django\ndjango.setup()' in command and record['output']['returncode']==0
    card=json.loads(SOURCE_CARD.read_text())['cards'][0]
    assert 'django.setup()' not in card['text']
    process=("In the recorded source checkout, an agent-local standalone forms reproducer "
        "used the following startup before importing forms:\nWorking directory: /testbed\n"
        "Shell environment: PYTHONPATH=tests:. DJANGO_SETTINGS_MODULE=test_sqlite\n"
        "Python startup:\nimport django\ndjango.setup()\nThe local reproducer then printed:\n"
        +record['output']['output']+"This records a source-local startup, not proof that the source patch "
        "was correct or that another process needs the same settings.")
    return {'source_instance_id':card['source_instance_id'],'source_run':str(SOURCE_RUN.relative_to(ROOT)),
        'source_agent_command_index_zero_based':17,'source_raw_command_index_zero_based':raw.index(record),'source_command_record':record,
        'source_command_file_sha256':sha(SOURCE_RUN/'commands.jsonl'),
        'source_procedure_file':str(SOURCE_CARD.relative_to(ROOT)),'source_procedure_file_sha256':sha(SOURCE_CARD),
        'source_procedure_original_sha256':card['text_sha256'],'process_observation':process,'procedure':card['text'],
        'shared_scope':"Historical source: django__django-11019, Django 3.0 checkout. Source official evaluation was incomplete and source resolved=false. Historical observations and tentative fixes are not current instructions; check applicability to the visible process state.",
        'selection_status':'selected_after_prior_target_analysis_diagnostic_only',
        'information_unit_definition':'E is a missing source startup observation; P is the existing tentative Media-fix procedure card. Not a generic evidence-versus-instruction style comparison. The two units have some shared outcome facts.'}


def prelude(mode):
    extra={'cold':'','environment_only':"sys.path.insert(0, '/testbed/tests')\nos.environ['DJANGO_SETTINGS_MODULE'] = 'test_sqlite'\n",
        'configured_not_ready':CUSTOM,'ready':CUSTOM+'django.setup()\n'}[mode]
    return BASE_PRELUDE+extra


def build(output):
    output=Path(output)
    if output.exists():raise FileExistsError(output)
    units=source_units();rows=[]
    for version,tag in IMAGES.items():
        info=json.loads(subprocess.check_output(['docker','image','inspect',tag],text=True))[0]
        for mode in MODES:
            prefix=prelude(mode)
            visible=('Current checkout: '+version+'. Working directory: /testbed.\n'
                'The current checkout contains /testbed/tests/test_sqlite.py.\n'
                'This exact prelude has ALREADY run successfully in the current process:\n'
                '```python\n'+prefix+'```\n\n'+PROBE_DESCRIPTION+
                '\nWrite only the additional Python startup needed before the fixed probe. If no additional startup is needed, return pass.')
            rows.append({'state_id':version+'--'+mode,'source_family':'django-11019-linked-diagnostic-family',
                'split':'diagnostic','mode':mode,'state_origin':'researcher_reconstructed_process_state_in_real_pinned_checkout',
                'target_previously_seen':True,'image_tag':tag,'image_id':info['Id'],'prelude':prefix,'state_text':visible,
                'preserve_configured_settings':mode in ('configured_not_ready','ready')})
    output.mkdir(parents=True)
    write(output/'source-units.json',units)
    (output/'states.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows))
    write(output/'design.json',{'created_at':datetime.now(timezone.utc).isoformat(),
        'purpose':'Feasibility and measurement pilot for source process information, not selector training or SWE-bench scoring.',
        'source_families':1,'repository_versions':2,'states':len(rows),'arms':['none','process','procedure','both','generic'],
        'primary_contrast':'both minus procedure; source startup observation added to unchanged procedure',
        'secondary_contrasts':['process minus none','both minus none','both minus generic'],
        'generic_definition':'same procedure plus general state-aware startup guidance, without source-specific module/path',
        'no_agent_outcome_based_replacements':True,
        'failure_categories':['syntax','startup_exception','settings_not_ready','app_registry_not_ready','probe_failure','existing_settings_changed','timeout','infrastructure'],
        'measures':['valid_fixed_probe','configuration_preserved','startup_exception','input_output_tokens','generation_seconds','docker_seconds','startup_probe_seconds'],
        'cost_limit':'one owned L4 runtime, 1800 s total controller limit, 1200 s script limit; no paid model API, no local GPU',
        'expansion_decision':'Preserve all outcomes. No trained-selector advantage claim from one connected family. If a fixed action or generic guidance suffices, report it and collect new independent sources before training.',
        'interpretation':'Content and length change together: package effects for a fixed worker and constructed states. Greedy repeats check reproducibility, not success-probability uncertainty.'})
    write(output/'artifact-index.json',{'sha256':{p.name:sha(p) for p in sorted(output.iterdir()) if p.is_file()}})
    return {'states':len(rows),'source_families':1,'output':str(output)}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    print(json.dumps(build(a.output)))
