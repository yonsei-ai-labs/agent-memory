"""Bounded inference-only worker. It never receives labels or executes code."""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import time
import torch
from huggingface_hub import hf_hub_download
from transformers import AutoTokenizer,Qwen3_5ForConditionalGeneration,StoppingCriteria,StoppingCriteriaList
from prepare_api_worker import validate_bundle,sha


def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


class Deadline(StoppingCriteria):
    def __init__(self,deadline):self.deadline=deadline
    def __call__(self,input_ids,scores,**kwargs):return time.monotonic()>=self.deadline


def checkpoint_provenance(config,output):
    def cached(name):return Path(hf_hub_download(config['model_id'],name,revision=config['revision'],local_files_only=True))
    raw=cached('config.json')
    if raw.parent.name!=config['revision']:raise ValueError('HF cache snapshot differs from requested revision')
    index=raw.parent/'model.safetensors.index.json'
    if index.is_file():names=sorted(set(json.loads(index.read_text())['weight_map'].values()))
    else:index=None;names=['model.safetensors']
    files={}
    for name in names:
        path=cached(name);digest=hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda:stream.read(8*1024*1024),b''):digest.update(block)
        files[name]={'sha256':digest.hexdigest(),'bytes':path.stat().st_size}
    record={'requested_revision':config['revision'],'cache_snapshot_revision':raw.parent.name,
            'raw_config_sha256':sha(raw),'weight_index_sha256':None if index is None else sha(index),'weight_files':files,
            'meaning':'Hashes of pinned local checkpoint shards used by the loader; not a hash of all in-memory tensors.'}
    write(output/'weight-provenance.json',record);return record


def run(config,data,output):
    jobs,protocol=validate_bundle(data,config)
    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported(including_emulation=False):
        raise RuntimeError('Dedicated native BF16 Colab GPU required')
    output.mkdir(parents=True,exist_ok=False);started=time.monotonic();deadline=started+config['script_budget_seconds']
    torch.set_num_threads(2);torch.manual_seed(731);torch.backends.cuda.matmul.allow_tf32=False
    tokenizer=AutoTokenizer.from_pretrained(config['model_id'],revision=config['revision'])
    tokenizer.save_pretrained(output/'tokenizer')
    prepared=[]
    for job in jobs:
        prompt=tokenizer.apply_chat_template(job['messages'],tokenize=False,add_generation_prompt=True,enable_thinking=False)
        ids=tokenizer(prompt,add_special_tokens=False)['input_ids']
        if len(ids)>config['max_input_tokens']:raise ValueError('Worker input exceeds untruncated budget')
        prepared.append((job,prompt,ids))
    model=Qwen3_5ForConditionalGeneration.from_pretrained(config['model_id'],revision=config['revision'],
        dtype=torch.bfloat16,attn_implementation='sdpa',device_map={'':'cuda:0'})
    model.eval().requires_grad_(False);model.config.save_pretrained(output/'model-config')
    if config.get('hash_checkpoint_shards',False):checkpoint_provenance(config,output)
    write(output/'environment.json',{'torch':torch.__version__,'cuda':torch.version.cuda,'gpu':torch.cuda.get_device_name(),
        'packages':{p:importlib.metadata.version(p) for p in ['transformers','accelerate','numpy']},
        'source_sha256':{n:sha(Path(n)) for n in ['generate_api_worker.py','prepare_api_worker.py']},
        'model_resolved_commit':getattr(model.config,'_commit_hash',None)})
    result={'status':'running','config':config,'protocol':protocol,'training_performed':False,'outcomes_evaluated':False,
            'generated_jobs':0,'expected_jobs':len(jobs),'repeat_checks':[]}
    write(output/'results.json',result);records={};native_features={};feature_costs={}
    with torch.inference_mode(),(output/'predictions.jsonl').open('w') as stream:
        for job,prompt,ids in prepared:
            if time.monotonic()>=deadline:raise TimeoutError('Worker inference deadline')
            tokens={'input_ids':torch.tensor([ids],device='cuda'),'attention_mask':torch.ones((1,len(ids)),dtype=torch.long,device='cuda')}
            if job['action']=='none' and job['repeat_of'] is None and config.get('collect_none_prefill_features',False):
                # Observable before any generated token or memory provision.
                # This diagnostic extra prefill is charged separately; it is
                # not silently treated as a free selector representation.
                torch.cuda.synchronize();feature_start=time.perf_counter()
                probe=model(**tokens,use_cache=False,output_hidden_states=True,logits_to_keep=1)
                value=probe.hidden_states[-1][0,-1].float()
                native_features[job['state_id']]=torch.nn.functional.normalize(value,dim=-1).cpu()
                del probe,value;torch.cuda.synchronize()
                feature_costs[job['state_id']]={'seconds':time.perf_counter()-feature_start,'input_tokens':len(ids),
                    'phase':'before_generated_tokens_and_without_memory','prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest()}
                torch.save(native_features,output/'none_prefill_features.pt');write(output/'none_prefill_feature_costs.json',feature_costs)
            torch.cuda.synchronize();tick=time.perf_counter()
            # Each generation creates a fresh cache. No state is shared between jobs.
            output_ids=model.generate(**tokens,max_new_tokens=config['max_new_tokens'],do_sample=False,
                use_cache=True,logits_to_keep=1,stopping_criteria=StoppingCriteriaList([Deadline(deadline)]),
                pad_token_id=tokenizer.pad_token_id,eos_token_id=tokenizer.eos_token_id)[0,len(ids):].cpu().tolist()
            torch.cuda.synchronize();seconds=time.perf_counter()-tick
            if time.monotonic()>=deadline:raise TimeoutError('Worker generation hit deadline; partial output excluded')
            record={k:job[k] for k in ['job_id','state_id','action','split','messages_sha256','repeat_of']}
            record.update(text=tokenizer.decode(output_ids,skip_special_tokens=True),input_tokens=len(ids),output_tokens=len(output_ids),
                input_token_ids=ids,output_token_ids=output_ids,rendered_prompt=prompt,
                rendered_prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),seconds=seconds,
                ended_with_eos=bool(output_ids and output_ids[-1]==tokenizer.eos_token_id),
                hit_token_limit=len(output_ids)>=config['max_new_tokens'])
            if job['repeat_of']:
                same=records[job['repeat_of']]['output_token_ids']==output_ids
                result['repeat_checks'].append({'job_id':job['job_id'],'same_output_tokens':same})
            records[job['job_id']]=record;stream.write(json.dumps(record,ensure_ascii=False)+'\n');stream.flush()
            result['generated_jobs']+=1;write(output/'results.json',result)
            if result['generated_jobs']%10==0:print(f"Worker {result['generated_jobs']}/{len(jobs)}; {time.monotonic()-started:.1f}s",flush=True)
    result.update(status='complete',seconds=time.monotonic()-started,peak_allocated_bytes=torch.cuda.max_memory_allocated(),
                  native_state_features=len(native_features),native_feature_extra_prefill_seconds=sum(v['seconds'] for v in feature_costs.values()))
    write(output/'results.json',result)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--config',type=Path,required=True);p.add_argument('--data',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);a=p.parse_args();run(json.loads(a.config.read_text()),a.data,a.output)
