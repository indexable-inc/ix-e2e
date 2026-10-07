#!/usr/bin/env python3
import argparse,json,os,subprocess,time,uuid,pathlib,sys
ROOT=pathlib.Path(__file__).resolve().parent
DATA=ROOT/'e2e-results.json'
STEPS=['ix new','wait running','command in VM','fork via snapshot/new','command in fork','delete both VMs']
def measure():
    runs=json.loads(DATA.read_text()) if DATA.exists() else []
    if not runs: print('0/6 lifecycle steps pass; first failing step: untested'); return
    steps=runs[-1]['steps']; passed=sum(s['pass'] for s in steps)
    failed=next((s for s in steps if not s['pass']),None)
    error=next((line for line in failed['error'].splitlines() if line.startswith('error:')),failed['error'].splitlines()[0] if failed and failed['error'] else '') if failed else ''
    first=failed['step']+': '+error if failed else 'none'
    print(f'{passed}/6 lifecycle steps pass; first failing step: {first}')
class Runner:
    def __init__(self,attempt):
        self.attempt=attempt; self.log=ROOT/f'ixdev-e2e-attempt-{attempt}'; self.log.mkdir(exist_ok=True)
        self.names=[]; self.rows=[]
    def cmd(self,label,args,timeout=180):
        start=time.monotonic()
        try:
            r=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
            result={'pass':r.returncode==0,'seconds':round(time.monotonic()-start,3),'error':r.stderr.strip() if r.returncode else '', 'stdout':r.stdout,'stderr':r.stderr,'returncode':r.returncode}
        except subprocess.TimeoutExpired as e:
            result={'pass':False,'seconds':round(time.monotonic()-start,3),'error':f'command timed out after {timeout}s','stdout':(e.stdout or b'').decode() if isinstance(e.stdout,bytes) else e.stdout or '', 'stderr':(e.stderr or b'').decode() if isinstance(e.stderr,bytes) else e.stderr or '', 'returncode':124}
        (self.log/f'{label}.json').write_text(json.dumps(result,indent=2))
        return result
    def inventory(self):
        r=self.cmd('inventory',['ix','ls','--output','json'],30)
        if not r['pass']: raise RuntimeError(r['error'])
        return json.loads(r['stdout'])['vms']
    def row(self,step,r):
        row={'step':step,'pass':r['pass'],'seconds':r['seconds'],'error':r['error']}
        self.rows.append(row); print(json.dumps(row),flush=True)
    def wait(self,name):
        start=time.monotonic(); last='absent'
        while time.monotonic()-start <120:
            vm=next((v for v in self.inventory() if v['name']==name),None)
            last=vm['status'] if vm else 'absent'
            if last=='running': return {'pass':True,'seconds':round(time.monotonic()-start,3),'error':''}
            if last in ('failed','stopped'): return {'pass':False,'seconds':round(time.monotonic()-start,3),'error':f'status={last}; '+(vm.get('failure_reason') or '')}
            time.sleep(2)
        return {'pass':False,'seconds':round(time.monotonic()-start,3),'error':f'wait deadline; status={last}'}
    def skip(self,step):self.row(step,{'pass':False,'seconds':0,'error':'blocked by earlier lifecycle failure'})
    def cleanup(self):
        start=time.monotonic(); errors=[]
        for name in self.names:
            for attempt in range(3):
                exists=next((v for v in self.inventory() if v['name']==name),None)
                if not exists:break
                result=self.cmd('delete-'+name,['ix','rm','-f',name],60)
                if not result['pass']:errors.append(result['error'])
        remaining=[v['name'] for v in self.inventory() if v['name'] in self.names]
        return {'pass':not remaining,'seconds':round(time.monotonic()-start,3),'error':('; '.join(errors)+'; remaining='+','.join(remaining)) if remaining else ''}
    def run(self):
        baseline=self.inventory(); (self.log/'baseline.json').write_text(json.dumps(baseline))
        source='ixdev-e2e-'+uuid.uuid4().hex[:12]; fork=source+'-fork'; self.names=[source,fork]
        try:
            r=self.cmd('new',['ix','new','--no-shell','--cpus','2','--name',source]); self.row(STEPS[0],r)
            if not r['pass']:return
            r=self.wait(source);self.row(STEPS[1],r)
            if not r['pass']:return
            command='set -e; uname -a; df -h; test "$(nproc)" -eq 2; printf ixdev-e2e > /root/ixdev-e2e-witness'
            r=self.cmd('source-command',['ix','shell',source,'--noninteractive','--','sh','-c',command]);self.row(STEPS[2],r)
            if not r['pass']:return
            start=time.monotonic();r=self.cmd('snapshot',['ix','snapshot','create',source,'--wait-durable'])
            if r['pass']:
                listing=self.cmd('snapshot-list',['ix','snapshot','ls',source,'--output','json']);r=listing
                if listing['pass']:
                    snapshots=json.loads(listing['stdout']); snapshots=snapshots.get('snapshots',[]) if isinstance(snapshots,dict) else snapshots
                    if not snapshots:r={'pass':False,'error':'snapshot list empty'}
                    else:
                        sid=snapshots[0]['id'];r=self.cmd('fork',['ix','new',sid,'--name',fork,'--no-shell'])
                        if r['pass']:r=self.wait(fork)
            r['seconds']=round(time.monotonic()-start,3);self.row(STEPS[3],r)
            if not r['pass']:return
            r=self.cmd('fork-command',['ix','shell',fork,'--noninteractive','--','sh','-c','set -e; uname -a; df -h; test "$(cat /root/ixdev-e2e-witness)" = ixdev-e2e']);self.row(STEPS[4],r)
        finally:
            while len(self.rows)<5:self.skip(STEPS[len(self.rows)])
            self.row(STEPS[5],self.cleanup())
            final=self.inventory();(self.log/'final.json').write_text(json.dumps(final))
            run={'attempt':self.attempt,'cli':subprocess.check_output(['ix','--version'],text=True).strip(),'names':self.names,'steps':self.rows,'created_remaining':[v['name'] for v in final if v['name'] in self.names],'baseline_count':len(baseline),'final_count':len(final)}
            runs=json.loads(DATA.read_text()) if DATA.exists() else [];runs.append(run);DATA.write_text(json.dumps(runs,indent=2));measure()
            table='| Step | Pass/fail | Seconds | Error text |\n|---|---|---:|---|\n'
            table += ''.join(f"| {s['step']} | {'PASS' if s['pass'] else 'FAIL'} | {s['seconds']:.3f} | {s['error'].replace(chr(10),' ').replace('|','/')} |\n" for s in self.rows)
            print(table, end='')
            (ROOT/'e2e-report.md').write_text(table)
            if os.environ.get('GITHUB_STEP_SUMMARY'):
                pathlib.Path(os.environ['GITHUB_STEP_SUMMARY']).write_text(table)
if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--measure',action='store_true');ap.add_argument('--attempt',type=int);args=ap.parse_args()
    if args.measure:measure()
    elif args.attempt:
        runner=Runner(args.attempt); runner.run()
        sys.exit(0 if runner.rows and all(s['pass'] for s in runner.rows) else 1)
