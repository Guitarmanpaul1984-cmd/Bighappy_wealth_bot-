import base64, json, os, time, urllib.parse, urllib.request
from pathlib import Path
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.transaction import VersionedTransaction

BASE = Path(__file__).with_name('bot(20260929-033135).py')
ns = {'__name__':'big_happy_base','__file__':str(BASE)}
exec(compile(BASE.read_text(encoding='utf-8'), str(BASE), 'exec'), ns)

WSOL = ns['WSOL']; USDC = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
OWNER = os.getenv('OWNER_TELEGRAM_CHAT_ID','').strip()
LIVE_ENABLED = os.getenv('LIVE_TRADING_ENABLED','0').lower() in {'1','true','on','yes'}
PRIV = os.getenv('SOLANA_TRADING_PRIVATE_KEY_B58','').strip()
JUP_KEY = os.getenv('JUPITER_API_KEY','').strip()
TREASURY = os.getenv('TREASURY_WALLET','').strip()
MAX_SIZE = float(os.getenv('MAX_LIVE_POSITION_SOL','0.10'))
MIN_SIZE = float(os.getenv('MIN_LIVE_POSITION_SOL','0.005'))
MIN_RESERVE = float(os.getenv('MIN_SOL_RESERVE','0.05'))
MIN_TRADING = float(os.getenv('MIN_TRADING_BALANCE_SOL','0.20'))
SWEEP_USD = float(os.getenv('PROFIT_SWEEP_USD','20'))
STATE_PATH = Path(os.getenv('LIVE_STATE_PATH','/data/live_state.json'))
PUBLIC_RPC = 'https://api.mainnet-beta.solana.com'
ALLOW_PUBLIC = os.getenv('ALLOW_PUBLIC_RPC_LIVE','0').lower() in {'1','true','on','yes'}
size_sol = min(float(os.getenv('LIVE_POSITION_SOL','0.02')), MAX_SIZE)
mode = 'paper'; selected = os.getenv('RISK_PROFILE','safest').lower()

PROFILES = {
 'safest':dict(label='1 — SAFEST',risk=20,liq=30000,age=5,market=50,stop=-.08,be=.06,be_stop=-.002,tp=(.08,.15,.25),parts=(.40,.30,.20),trail=.07,time=10,time_max=.02,opens=1,slip=250,impact=.02,wallet=.02,daily=.02,losses=2,priority=300000),
 'balanced':dict(label='2 — BALANCED',risk=35,liq=10000,age=2,market=30,stop=-.12,be=.08,be_stop=-.005,tp=(.10,.20,.35),parts=(.30,.30,.20),trail=.10,time=15,time_max=.03,opens=2,slip=400,impact=.04,wallet=.05,daily=.05,losses=3,priority=600000),
 'risky':dict(label='3 — RISKY',risk=50,liq=5000,age=1,market=20,stop=-.18,be=.12,be_stop=-.01,tp=(.15,.30,.60),parts=(.25,.25,.25),trail=.15,time=20,time_max=.04,opens=3,slip=800,impact=.08,wallet=.10,daily=.08,losses=4,priority=1000000),
}
if selected not in PROFILES: selected='safest'
positions={}; events=[]; profit_bucket=0.0; consecutive_losses=0; halt=None; start_balance=None
def state_storage_ready():
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        probe = STATE_PATH.parent / '.bhwb_write_test'
        probe.write_text('ok', encoding='utf-8')
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False

def load_state():
    global positions, events, profit_bucket, consecutive_losses, halt, start_balance, selected, size_sol
    if not STATE_PATH.exists():
        return
    try:
        data=json.loads(STATE_PATH.read_text(encoding='utf-8'))
        positions=data.get('positions') or {}
        events=data.get('events') or []
        profit_bucket=float(data.get('profit_bucket') or 0.0)
        consecutive_losses=int(data.get('consecutive_losses') or 0)
        halt=data.get('halt')
        start_balance=data.get('start_balance')
        if data.get('selected') in PROFILES:selected=data['selected']
        saved_size=float(data.get('size_sol') or size_sol)
        if MIN_SIZE<=saved_size<=MAX_SIZE:size_sol=saved_size
        print(f'LIVE STATE LOADED positions={len(positions)} events={len(events)}', flush=True)
    except Exception as e:
        print(f'LIVE STATE LOAD ERROR {type(e).__name__}: {e}', flush=True)

def save_state():
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload={
            'positions':positions,'events':events[-500:],'profit_bucket':profit_bucket,
            'consecutive_losses':consecutive_losses,'halt':halt,'start_balance':start_balance,
            'selected':selected,'size_sol':size_sol,'saved_at':time.time(),
        }
        tmp=STATE_PATH.with_suffix('.tmp')
        tmp.write_text(json.dumps(payload,separators=(',',':')),encoding='utf-8')
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        print(f'LIVE STATE SAVE ERROR {type(e).__name__}: {e}', flush=True)

load_state()
kp = None
if PRIV:
    try: kp=Keypair.from_base58_string(PRIV)
    except Exception: print('LIVE WALLET CONFIG ERROR: invalid secret', flush=True)

def p(): return PROFILES[selected]
def owner_ok(cid):
    try:return int(cid)==int(OWNER)
    except:return False
def tell(text):
    if OWNER:
        try: ns['send_message'](int(OWNER), text)
        except: pass

def apply_profile():
    q=p(); ns.update(RISK_MAX=q['risk'],MIN_LIQUIDITY_USD=q['liq'],MIN_PAIR_AGE_MIN=q['age'],MARKET_MIN=q['market'],HARD_STOP_NET=q['stop'],BREAKEVEN_ARM_NET=q['be'],BREAKEVEN_STOP_NET=q['be_stop'],TP1_NET=q['tp'][0],TP2_NET=q['tp'][1],TP3_NET=q['tp'][2],TP1_FRACTION=q['parts'][0],TP2_FRACTION=q['parts'][1],TP3_FRACTION=q['parts'][2],RUNNER_TRAIL=q['trail'],TIME_STOP_MIN=q['time'],TIME_STOP_MAX_NET=q['time_max'],MAX_OPEN=q['opens'],MAX_CONSECUTIVE_LOSSES=q['losses'],DAILY_LOSS_LIMIT=q['daily'])
apply_profile()
save_state()

def rpc(method, params):
    body=json.dumps({'jsonrpc':'2.0','id':9,'method':method,'params':params}).encode()
    out=ns['http_json'](ns['SOLANA_RPC'],data=body,headers={'Content-Type':'application/json'},timeout=25)
    return None if not out or out.get('error') else out.get('result')
def pub(): return str(kp.pubkey()) if kp else None
def balance():
    r=rpc('getBalance',[pub(),{'commitment':'confirmed'}]) if pub() else None
    return None if not r else float(r.get('value',0))/1e9
def token_bal(mint):
    r=rpc('getTokenAccountsByOwner',[pub(),{'mint':mint},{'encoding':'jsonParsed','commitment':'confirmed'}]) if pub() else None
    if r is None:return None
    total=0
    for x in r.get('value',[]):
        try: total+=int(x['account']['data']['parsed']['info']['tokenAmount']['amount'])
        except: pass
    return total

def ready():
    if not LIVE_ENABLED:return False,'LIVE_TRADING_ENABLED is OFF'
    if not OWNER:return False,'OWNER_TELEGRAM_CHAT_ID missing'
    if not kp:return False,'trading-wallet secret missing'
    if not state_storage_ready():return False,'persistent /data state storage is unavailable'
    if ns['SOLANA_RPC']==PUBLIC_RPC and not ALLOW_PUBLIC:return False,'dedicated SOLANA_RPC_URL required'
    return True,'READY'

def jheaders(content=False):
    h={'Accept':'application/json','User-Agent':'BigHappyWealthBot/Live'}
    if JUP_KEY:h['x-api-key']=JUP_KEY
    if content:h['Content-Type']='application/json'
    return h
def jbase(): return 'https://api.jup.ag' if JUP_KEY else 'https://lite-api.jup.ag'
def quote(inp,out,amt,slip=None):
    q=urllib.parse.urlencode({'inputMint':inp,'outputMint':out,'amount':str(int(amt)),'slippageBps':str(int(slip or p()['slip'])),'swapMode':'ExactIn','restrictIntermediateTokens':'true','instructionVersion':'V2'})
    return ns['http_json'](f'{jbase()}/swap/v1/quote?{q}',headers=jheaders(),timeout=20)
def confirm(sig):
    end=time.time()+45
    while time.time()<end:
        r=rpc('getSignatureStatuses',[[sig],{'searchTransactionHistory':True}])
        row=(r.get('value') or [None])[0] if r else None
        if row:
            if row.get('err') is not None:return False
            if row.get('confirmationStatus') in {'confirmed','finalized'}:return True
        time.sleep(1.2)
    return False

def swap(inp,out,amt):
    ok,why=ready()
    if not ok:return None,why
    q=quote(inp,out,amt)
    if not q or q.get('error') or not q.get('outAmount'):return None,'quote failed'
    try: impact=abs(float(q.get('priceImpactPct') or 0))
    except: impact=1
    if impact>p()['impact']:return None,f'price impact {impact*100:.1f}% too high'
    body={'userPublicKey':pub(),'quoteResponse':q,'wrapAndUnwrapSol':True,'dynamicComputeUnitLimit':True,'prioritizationFeeLamports':{'priorityLevelWithMaxLamports':{'priorityLevel':'veryHigh','maxLamports':p()['priority']}}}
    built=ns['http_json'](f'{jbase()}/swap/v1/swap',data=json.dumps(body).encode(),headers=jheaders(True),timeout=30)
    if not built or not built.get('swapTransaction'):return None,'swap build failed'
    try:
        tx=VersionedTransaction.from_bytes(base64.b64decode(built['swapTransaction']))
        signed=VersionedTransaction(tx.message,[kp]); b64=base64.b64encode(bytes(signed)).decode()
    except Exception as e:return None,f'signing failed: {type(e).__name__}'
    sim=rpc('simulateTransaction',[b64,{'encoding':'base64','sigVerify':True,'commitment':'confirmed'}])
    if sim is None or (sim.get('value') or {}).get('err') is not None:return None,'simulation rejected'
    before=balance(); out_before=token_bal(out) if out!=WSOL else None
    sig=rpc('sendTransaction',[b64,{'encoding':'base64','skipPreflight':False,'preflightCommitment':'confirmed','maxRetries':3}])
    if not isinstance(sig,str) or not confirm(sig):return None,'transaction not confirmed'
    after=balance(); out_after=token_bal(out) if out!=WSOL else None
    return {'sig':sig,'quote':q,'before':before,'after':after,'out_before':out_before,'out_after':out_after},None

def strict_safety(mint):
    old=ns['paper_test_mode']; ns['paper_test_mode']=False
    try:return ns['onchain_safety'](mint)
    finally:ns['paper_test_mode']=old

def candidate_ok(item):
    pair=item['pair']; q=p()
    try:liq=float((pair.get('liquidity') or {}).get('usd') or 0)
    except:liq=0
    _,market,age=ns['market_scores'](pair)
    if liq<q['liq']:return False,'liquidity'
    if age<q['age']:return False,'age'
    if market<q['market']:return False,'market score'
    safe,why=strict_safety(item['mint'])
    if not safe:return False,why or 'strict safety'
    if safe.get('risk',999)>q['risk']:return False,'risk score'
    return True,safe

def entry_size():
    b=balance()
    if b is None:return 0
    return max(0,min(size_sol,MAX_SIZE,b*p()['wallet'],b-MIN_RESERVE-MIN_TRADING))
def day_pnl():
    d=time.strftime('%Y-%m-%d',time.gmtime()); return sum(e['pnl'] for e in events if e['day']==d)
def breaker():
    global halt
    if halt:return halt
    base=start_balance or balance() or 0
    if consecutive_losses>=p()['losses']:halt=f'{consecutive_losses} consecutive losses'
    elif base and day_pnl()<=-(base*p()['daily']):halt=f'daily loss limit {p()["daily"]*100:.0f}%'
    if halt:tell('🛑 LIVE CIRCUIT BREAKER\n'+halt+'\nNew real entries are blocked.')
    return halt

def open_live(item):
    mint=item['mint']; ok,why=candidate_ok(item)
    if not ok:return False,why
    amt=entry_size()
    if amt<MIN_SIZE:return False,'not enough spendable SOL after reserves/profile cap'
    before_tok=token_bal(mint) or 0; r,err=swap(WSOL,mint,int(amt*1e9))
    if not r:return False,err
    after_tok=token_bal(mint); got=max(0,int(after_tok or 0)-int(before_tok))
    if got<=0:got=int(r['quote'].get('outAmount') or 0)
    if got<=0:return False,'could not measure tokens received'
    spent=amt
    if r['before'] is not None and r['after'] is not None and r['before']>r['after']:spent=r['before']-r['after']
    try:liq=float((item['pair'].get('liquidity') or {}).get('usd') or 0)
    except:liq=0
    positions[mint]={'mint':mint,'symbol':item.get('symbol','?'),'initial_raw':got,'raw':got,'cost':spent,'remaining_cost':spent,'opened':time.time(),'entry_liq':liq,'peak':-999.,'be':False,'tp':[False,False,False]}
    save_state()
    tell(f"🟣 REAL ENTRY\n{item.get('symbol','?')}\nSpent {spent:.6f} SOL\nProfile {p()['label']}\nTx {r['sig']}")
    return True,r['sig']

def record_pnl(x):
    global profit_bucket,consecutive_losses
    events.append({'day':time.strftime('%Y-%m-%d',time.gmtime()),'pnl':x}); profit_bucket+=x
    consecutive_losses = consecutive_losses+1 if x<0 else 0
    breaker(); save_state()
def sell(mint,raw,reason):
    pos=positions.get(mint)
    if not pos:return False
    raw=min(int(raw),int(pos['raw'])); frac=raw/pos['raw']; basis=pos['remaining_cost']*frac
    r,err=swap(mint,WSOL,raw)
    if not r:
        tell(f'⚠️ REAL EXIT FAILED {pos["symbol"]}: {err}'); return False
    received=max(0,(r['after'] or 0)-(r['before'] or 0)) if r['before'] is not None and r['after'] is not None else int(r['quote']['outAmount'])/1e9
    pnl=received-basis; pos['raw']-=raw; pos['remaining_cost']-=basis; record_pnl(pnl)
    tell(f"🟣 REAL EXIT\n{pos['symbol']} — {reason}\nReceived {received:.6f} SOL\nP&L {pnl:+.6f} SOL\nTx {r['sig']}")
    if pos['raw']<=max(1,int(pos['initial_raw']*.002)):positions.pop(mint,None)
    save_state(); maybe_sweep(); return True

def current_net(pos):
    q=quote(pos['mint'],WSOL,pos['raw'])
    if not q or not q.get('outAmount') or pos['remaining_cost']<=0:return None
    return (int(q['outAmount'])/1e9-pos['remaining_cost'])/pos['remaining_cost']
def exits():
    for mint,pos in list(positions.items()):
        net=current_net(pos)
        if net is None:continue
        q=p(); pos['peak']=max(pos['peak'],net); age=(time.time()-pos['opened'])/60
        pair=ns['best_pumpswap_pair'](mint)
        if pair:
            try:liq=float((pair.get('liquidity') or {}).get('usd') or 0)
            except:liq=0
            if pos['entry_liq'] and liq<pos['entry_liq']*ns['LIQUIDITY_COLLAPSE_RATIO']:
                sell(mint,pos['raw'],'liquidity collapse'); continue
        if net<=q['stop']:sell(mint,pos['raw'],f'hard stop {net*100:.1f}%'); continue
        if not pos['be'] and net>=q['be']:pos['be']=True
        if pos['be'] and net<=q['be_stop']:sell(mint,pos['raw'],'breakeven protection'); continue
        for i,target in enumerate(q['tp']):
            if not pos['tp'][i] and net>=target:
                amt=min(pos['raw'],max(1,int(pos['initial_raw']*q['parts'][i])))
                if sell(mint,amt,f'TP{i+1}') and mint in positions:positions[mint]['tp'][i]=True
                break
        else:
            if all(pos['tp']) and net<=pos['peak']-q['trail']:sell(mint,pos['raw'],'runner trail')
            elif age>=q['time'] and net<=q['time_max']:sell(mint,pos['raw'],'time stop')

def maybe_entries():
    if mode!='live' or breaker() or not ready()[0] or len(positions)>=p()['opens']:return
    for item in ns.get('candidates') or []:
        if len(positions)>=p()['opens']:break
        if item['mint'] not in positions:open_live(item)
def sol_usd():
    q=quote(WSOL,USDC,50_000_000,100); return (int(q['outAmount'])/1e6)/.05 if q and q.get('outAmount') else None
def send_sol(lamports):
    if not TREASURY:return None
    try:to=Pubkey.from_string(TREASURY); bh=rpc('getLatestBlockhash',[{'commitment':'confirmed'}])['value']['blockhash']
    except:return None
    ix=transfer(TransferParams(from_pubkey=kp.pubkey(),to_pubkey=to,lamports=int(lamports)))
    msg=MessageV0.try_compile(kp.pubkey(),[ix],[],Hash.from_string(bh)); tx=VersionedTransaction(msg,[kp]); b64=base64.b64encode(bytes(tx)).decode()
    sig=rpc('sendTransaction',[b64,{'encoding':'base64','skipPreflight':False,'preflightCommitment':'confirmed','maxRetries':3}])
    return sig if isinstance(sig,str) and confirm(sig) else None
def maybe_sweep():
    global profit_bucket
    if not TREASURY or profit_bucket<=0:return
    px=sol_usd()
    if not px:return
    need=SWEEP_USD/px
    if profit_bucket<need:return
    b=balance()
    if b is None or b-need<MIN_RESERVE+MIN_TRADING:return
    sig=send_sol(int(need*1e9))
    if sig:
        profit_bucket=max(0,profit_bucket-need); save_state(); tell(f'🏦 PROFIT SWEEP\nSent ~${SWEEP_USD:.0f} ({need:.6f} SOL) to allowlisted treasury.\nTx {sig}')

def profiles_text():
    return '\n'.join(['🎚 RISK PROFILES']+[f"{PROFILES[k]['label']}: stop {PROFILES[k]['stop']*100:.0f}% | TP {PROFILES[k]['tp'][0]*100:.0f}/{PROFILES[k]['tp'][1]*100:.0f}/{PROFILES[k]['tp'][2]*100:.0f}% | liq ${PROFILES[k]['liq']:,.0f}+ | {PROFILES[k]['wallet']*100:.0f}% wallet/trade" for k in ('safest','balanced','risky')]+['Risky targets more upside but does not guarantee higher returns.'])
def settings():
    ok,why=ready(); b=balance() if kp else None
    return f"⚙️ SETTINGS\nMode: {mode.upper()}\nProfile: {p()['label']}\nRequested size: {size_sol:.4f} SOL\nOpen REAL: {len(positions)}/{p()['opens']}\nLive: {'READY' if ok else 'LOCKED — '+why}"+(f"\nWallet balance: {b:.6f} SOL" if b is not None else '')+f"\nProfit sweep: ${SWEEP_USD:.0f}"

base_handle=ns['handle_message']; base_scan=ns['run_scan']
def dual_scan():
    if positions:exits()
    out=base_scan()
    if mode=='live':maybe_entries()
    return out
ns['run_scan']=dual_scan

def handle(cid,text):
    global mode,size_sol,selected,halt,consecutive_losses,start_balance
    raw=(text or '').strip(); a=raw.split(); cmd=a[0].lower() if a else ''
    protected={'/mode','/size','/riskprofile','/settings','/wallet','/livepositions','/liveperformance','/panic','/liveresume','/sweep'}
    if cmd in protected and not owner_ok(cid):ns['send_message'](cid,'🔒 Live-trading controls are owner-locked.'); return
    if cmd=='/mode':
        opt=a[1].lower() if len(a)>1 else 'status'
        if opt=='paper':mode='paper'; save_state(); ns['send_message'](cid,'🧪 PAPER MODE. New real entries are OFF; any open real positions remain risk-managed.'); return
        if opt=='live':
            if len(a)<3 or a[2].upper()!='CONFIRM':ns['send_message'](cid,'Send exactly: /mode live CONFIRM'); return
            ok,why=ready(); b=balance()
            if not ok or b is None or b<MIN_RESERVE+MIN_TRADING+MIN_SIZE:ns['send_message'](cid,'🔒 LIVE NOT READY\n'+(why if not ok else 'wallet balance too low')); return
            mode='live'; start_balance=start_balance or b; save_state(); ns['send_message'](cid,f"🟣 LIVE MODE ARMED\nProfile {p()['label']}\nRequested size {size_sol:.4f} SOL\nPaper tracking stays active too."); return
        ns['send_message'](cid,f'Mode: {mode.upper()}'); return
    if cmd=='/size':
        if len(a)<2:ns['send_message'](cid,f'Size: {size_sol:.4f} SOL. Example /size 0.02'); return
        try:x=float(a[1])
        except:ns['send_message'](cid,'Invalid size.'); return
        if not MIN_SIZE<=x<=MAX_SIZE:ns['send_message'](cid,f'Allowed: {MIN_SIZE:.4f}-{MAX_SIZE:.4f} SOL'); return
        size_sol=x; ns['POSITION_SOL']=x; save_state(); ns['send_message'](cid,f'✅ Trade size set to {x:.4f} SOL. Profile wallet caps can reduce live size further.'); return
    if cmd=='/riskprofile':
        aliases={'1':'safest','safe':'safest','safest':'safest','2':'balanced','balanced':'balanced','3':'risky','risky':'risky'}
        opt=aliases.get(a[1].lower()) if len(a)>1 else None
        if not opt:ns['send_message'](cid,profiles_text()); return
        selected=opt; apply_profile(); save_state(); ns['send_message'](cid,'✅ '+p()['label']+' selected.\n'+profiles_text()); return
    if cmd=='/settings':ns['send_message'](cid,settings()); return
    if cmd=='/wallet':
        ok,why=ready(); b=balance() if kp else None; address=pub() or 'not configured'
        ns['send_message'](cid,f"👛 LIVE WALLET\nAddress: {address}\n"+(f"Balance: {b:.6f} SOL\n" if b is not None else '')+f"State: {'READY' if ok else 'LOCKED — '+why}\nPrivate key is never displayed."); return
    if cmd=='/livepositions':
        lines=['🟣 REAL POSITIONS']+[f"{x['symbol']} | {x['remaining_cost']:.4f} SOL cost left" for x in positions.values()]
        ns['send_message'](cid,'\n'.join(lines) if positions else 'No open REAL positions.'); return
    if cmd=='/liveperformance':
        pnl=sum(e['pnl'] for e in events); ns['send_message'](cid,f'🟣 REAL PERFORMANCE\nRealized P&L {pnl:+.6f} SOL\nToday {day_pnl():+.6f} SOL\nSweep bucket {profit_bucket:+.6f} SOL\nCircuit {halt or "READY"}'); return
    if cmd=='/sweep':
        px=sol_usd() if kp else None; usd=profit_bucket*px if px else None
        ns['send_message'](cid,f'🏦 SWEEP\nThreshold ${SWEEP_USD:.0f}\nBucket {profit_bucket:+.6f} SOL'+(f' (~${usd:.2f})' if usd is not None else '')+f"\nTreasury {'configured' if TREASURY else 'not configured'}"); return
    if cmd=='/liveresume':
        if len(a)<2 or a[1].upper()!='CONFIRM':ns['send_message'](cid,'Use /liveresume CONFIRM'); return
        halt=None; consecutive_losses=0; save_state(); ns['send_message'](cid,'✅ Live circuit breaker reset.'); return
    if cmd=='/panic':
        if len(a)<2 or a[1].upper()!='CONFIRM':ns['send_message'](cid,'Emergency close: /panic CONFIRM'); return
        mode='paper'; save_state(); ns['send_message'](cid,'🚨 PANIC EXIT — real entries OFF; closing all real positions.')
        for mint,x in list(positions.items()):sell(mint,x['raw'],'PANIC EXIT')
        return
    if cmd=='/risk':ns['send_message'](cid,'Selected '+p()['label']+'\n'+profiles_text()); ns['send_message'](cid,ns['risk_text']()); return
    if cmd=='/help':
        base_handle(cid,text); ns['send_message'](cid,'Live controls (OWNER ONLY):\n/settings\n/size 0.02\n/riskprofile 1|2|3\n/mode paper\n/mode live CONFIRM\n/wallet\n/livepositions\n/liveperformance\n/sweep\n/panic CONFIRM\n/liveresume CONFIRM'); return
    base_handle(cid,text)
ns['handle_message']=handle
print(f"DUAL ENGINE STARTUP mode={mode} profile={selected} live_ready={ready()[0]} state={STATE_PATH}",flush=True)
if __name__=='__main__':ns['main']()
