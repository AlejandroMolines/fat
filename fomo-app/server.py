from __future__ import annotations
import json, os, sys, threading, uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen

APP = Path(__file__).resolve().parent
DATA = APP / 'data'; DATA.mkdir(exist_ok=True)
from engine import Config, Engine, EngineError, FxQuote, Instrument, PriceQuote, Signal
from fomo_integration import FomoFeed, iso_from_event

STATE, QUOTES, LABELS = DATA/'account.json', DATA/'quotes.json', DATA/'asset_labels.json'
LOCK = threading.RLock()
def utc(): return datetime.now(timezone.utc)
def stamp(t=None): return (t or utc()).isoformat(timespec='seconds').replace('+00:00','Z')
def dec(x, name):
    try: n=Decimal(str(x))
    except (InvalidOperation,ValueError,TypeError): raise ValueError(f'{name}: número no válido') from None
    if not n.is_finite(): raise ValueError(f'{name}: valor no válido')
    return n
def fresh(): return Engine(Config())
engine=Engine.load(str(STATE)) if STATE.exists() else fresh()
quotes=json.loads(QUOTES.read_text(encoding='utf-8')) if QUOTES.exists() else {}
asset_labels=json.loads(LABELS.read_text(encoding='utf-8')) if LABELS.exists() else {}
def load_api_key():
    key=os.environ.get('FOMOAPI_KEY','').strip()
    if key: return key
    try:
        for line in (DATA/'fomo.env').read_text(encoding='utf-8').splitlines():
            if line.strip().startswith('FOMOAPI_KEY='): return line.split('=',1)[1].strip().strip('"\'')
    except OSError: pass
    return ''
def save():
    engine.save(str(STATE)); QUOTES.write_text(json.dumps(quotes,ensure_ascii=False,indent=2),encoding='utf-8'); LABELS.write_text(json.dumps(asset_labels,ensure_ascii=False,indent=2),encoding='utf-8')
def marks():
    result={}
    for a,q in quotes.items():
        if a not in engine.instruments: continue
        fx=FxQuote('USD',Decimal(q['fx_rate']),q['fx_as_of'],q['fx_source']) if q.get('fx_rate') else None
        result[a]=(PriceQuote(a,Decimal(q['price']),q['as_of'],q['source']),fx)
    return result
fx_cache={}
def fx_usd_eur():
    if fx_cache.get('quote') and (datetime.now(timezone.utc)-fx_cache['fetched']).total_seconds()<21600: return fx_cache['quote']
    with urlopen('https://api.frankfurter.dev/v2/rate/USD/EUR',timeout=12) as response: body=json.loads(response.read().decode('utf-8'))
    quote=FxQuote('USD',Decimal(str(body['rate'])),str(body['date'])+'T00:00:00Z','Frankfurter v2 · referencia diaria')
    fx_cache.update({'quote':quote,'fetched':datetime.now(timezone.utc)}); return quote
def find_price(value):
    if isinstance(value,dict):
        token=value.get('token')
        if isinstance(token,dict) and token.get('priceUsd') is not None: return token['priceUsd']
        if value.get('priceUsd') is not None: return value['priceUsd']
        for k in ('data','trade','position','details'):
            if k in value:
                found=find_price(value[k])
                if found is not None: return found
    return None
def iso_now(): return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
fomo_feed=None
def process_fomo_alert(event):
    global asset_labels
    user=str(event.get('userId') or ''); address=str(event.get('tokenAddress') or '').strip(); chain_id=str(event.get('chainId') or '')
    trade_id=str(event.get('tradeId') or ''); side=str(event.get('_side') or '')
    if not user or not address or not chain_id or not trade_id or side not in ('BUY','SELL'): return
    price_value=event.get('priceUsd')
    if price_value is None: price_value=find_price(fomo_feed.api_get('/v2/trades/'+trade_id))
    if price_value is None: return
    price=Decimal(str(price_value))
    if not price.is_finite() or price<=0: return
    fx=fx_usd_eur(); detected=iso_now(); original=iso_from_event(event.get('_event_time')) or detected
    asset=f"FOMO-{user[:12]}-{chain_id}-{address.lower()}"; source='fomoapi:alert-feed + trade-detail'
    with LOCK:
        if asset not in engine.instruments: engine.register_instrument(Instrument(asset,'USD'))
        asset_labels[asset]={'symbol':str(event.get('token') or 'TOKEN'),'chain':str(event.get('chain') or chain_id),'address':address,'investor':str(event.get('trader') or user)}
        signal=Signal('FOMO-'+str(event.get('eventId')),user,asset,side,Decimal(str(event.get('tradeUsd') or 0)),original,detected,source,'OK')
        engine.apply_signal(signal,PriceQuote(asset,price,detected,'fomoapi:price-at-detection'),fx=fx)
        quotes[asset]={'price':str(price),'as_of':detected,'source':'fomoapi:price-at-detection','fx_rate':str(fx.rate),'fx_as_of':fx.as_of,'fx_source':fx.source}
        save()
fomo_feed=FomoFeed(load_api_key(),process_fomo_alert,DATA,int(os.environ.get('FOMO_COPY_TOP_N','5')))
def state():
    s=engine.snapshot(marks(),stamp()); s['ledger_digest']=engine.ledger_digest(); s['assets']=asset_labels
    f=dict(fomo_feed.state); f['auto_copy']=fomo_feed.auto_copy; f['investors']=fomo_feed.investors
    s['meta']={'mode':'paper','currency':'EUR','demo':True,'feed_connected':f['connected'],'feed_label':f['message'],'ledger_digest':s['ledger_digest'],'fomo':f}; return s

class Handler(BaseHTTPRequestHandler):
    def log_message(self,fmt,*args): print('[%s] %s'%(self.log_date_time_string(),fmt%args))
    def reply(self,obj,status=200,ctype='application/json; charset=utf-8'):
        b=json.dumps(obj,ensure_ascii=False).encode() if ctype.startswith('application/json') else obj
        self.send_response(status); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(b))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(b)
    def body(self):
        n=int(self.headers.get('Content-Length','0'))
        if not 0<n<100_000: raise ValueError('Solicitud vacía o demasiado grande')
        try: return json.loads(self.rfile.read(n))
        except json.JSONDecodeError: raise ValueError('JSON no válido') from None
    def do_GET(self):
        p=urlparse(self.path).path
        if p=='/api/state':
            with LOCK: self.reply(state())
        elif p=='/api/health': self.reply({'ok':True,'mode':'paper','feed_connected':fomo_feed.state['connected']})
        elif p in ('/','/index.html'): self.static('index.html','text/html; charset=utf-8')
        elif p=='/app.js': self.static('app.js','text/javascript; charset=utf-8')
        elif p=='/styles.css': self.static('styles.css','text/css; charset=utf-8')
        else: self.send_error(404)
    def static(self,name,typ):
        f=APP/'web'/name
        if not f.is_file(): self.send_error(404); return
        self.reply(f.read_bytes(),ctype=typ)
    def do_POST(self):
        global engine,quotes,asset_labels
        p=urlparse(self.path).path
        try:
            x=self.body()
            with LOCK:
                if p=='/api/signals':
                    asset=str(x.get('asset','')).strip().upper(); investor=str(x.get('investor_id','')).strip(); side=str(x.get('direction','')).upper(); source=str(x.get('source','')).strip()
                    if not asset or len(asset)>24 or not investor or len(investor)>80: raise ValueError('Indica activo e inversor')
                    if side not in ('BUY','SELL') or not source: raise ValueError('Dirección BUY/SELL y fuente obligatorias')
                    if str(x.get('currency','EUR')).upper()!='EUR': raise ValueError('Esta versión solo admite activos cotizados en EUR')
                    if asset not in engine.instruments: engine.register_instrument(Instrument(asset,'EUR'))
                    detected=str(x.get('detected_at') or stamp()); original=str(x.get('original_at') or detected); price=dec(x.get('price'),'Precio')
                    if price<=0: raise ValueError('El precio debe ser positivo')
                    sid='MAN-'+uuid.uuid4().hex[:12]
                    sig=Signal(sid,investor,asset,side,dec(x.get('observed_size',0),'Tamaño'),original,detected,source,'OK')
                    rec=engine.apply_signal(sig,PriceQuote(asset,price,detected,source))
                    if rec.state!='RECHAZADA': quotes[asset]={'price':str(price),'as_of':detected,'source':source}
                    save(); self.reply({'ok':rec.state!='RECHAZADA','record':{'state':rec.state,'reason_code':rec.reason_code,'reason_text':rec.reason_text,'signal_id':sid},'state':state()},201); return
                if p=='/api/quotes':
                    asset=str(x.get('asset','')).upper(); price=dec(x.get('price'),'Precio')
                    if asset not in engine.instruments or price<=0: raise ValueError('Activo desconocido o precio no válido')
                    at=stamp(); source=str(x.get('source') or 'precio manual'); stored_price=price; fx=None
                    if engine.instruments[asset].currency=='USD':
                        fx=fx_usd_eur(); stored_price=price/fx.rate
                    quotes[asset]={'price':str(stored_price),'as_of':at,'source':source,**({'fx_rate':str(fx.rate),'fx_as_of':fx.as_of,'fx_source':fx.source} if fx else {})}; save(); self.reply({'ok':True,'state':state()}); return
                if p=='/api/fomo/toggle':
                    fomo_feed.auto_copy=bool(x.get('enabled'))
                    fomo_feed.state['message']='Copia automática activa' if fomo_feed.auto_copy else 'Feed conectado · copia automática pausada'
                    self.reply({'ok':True,'state':state()}); return
                if p=='/api/fomo/refresh':
                    if not fomo_feed.key: raise ValueError('Configura FOMOAPI_KEY localmente primero')
                    count=fomo_feed.refresh_leaderboard(); fomo_feed.state.update({'connected':True,'message':'Ranking actualizado','last_poll_at':stamp(),'last_error':None,'leaderboard_count':count})
                    self.reply({'ok':True,'state':state()}); return
                if p=='/api/fomo/settings':
                    windows=x.get('windows',[]); top_n=int(x.get('top_n',5))
                    if not isinstance(windows,list) or not windows or any(w not in ('24h','7d','30d','all') for w in windows): raise ValueError('Selecciona al menos un periodo válido')
                    if not 1<=top_n<=20: raise ValueError('El número de inversores debe estar entre 1 y 20')
                    fomo_feed.set_preferences(windows,top_n)
                    count=fomo_feed.refresh_leaderboard() if fomo_feed.key else 0
                    fomo_feed.state.update({'leaderboard_count':count,'message':'Preferencias guardadas','last_error':None})
                    self.reply({'ok':True,'state':state()}); return
                if p=='/api/close':
                    copy=engine.copies.get(str(x.get('copy_id','')))
                    if not copy: raise ValueError('Posición no encontrada')
                    manual_price=x.get('price') not in (None,'')
                    saved=quotes.get(copy.asset,{})
                    price=dec(x.get('price') if manual_price else saved.get('price'),'Precio de salida')
                    if price<=0: raise ValueError('Precio de salida no válido')
                    at=stamp(); source=str(x.get('source') or 'precio manual de salida')
                    fx=None
                    if engine.instruments[copy.asset].currency=='USD':
                        fx=FxQuote('USD',Decimal('1'),at,'precio de salida introducido en EUR') if manual_price else FxQuote('USD',Decimal(saved['fx_rate']),saved['fx_as_of'],saved['fx_source'])
                    engine.close_copy(copy.copy_id,PriceQuote(copy.asset,price,at,source),at=at,fx=fx); quotes[copy.asset]={'price':str(price),'as_of':at,'source':source,**({'fx_rate':str(fx.rate),'fx_as_of':fx.as_of,'fx_source':fx.source} if fx else {})}; save(); self.reply({'ok':True,'state':state()}); return
                if p=='/api/reset':
                    engine=fresh(); quotes={}; asset_labels={}; save(); self.reply({'ok':True,'state':state()}); return
                if p=='/api/demo':
                    engine=fresh(); quotes={}; asset_labels={}; engine.register_instrument(Instrument('DEMO','EUR')); t=utc(); a=stamp(t); b=stamp(t+timedelta(seconds=1)); src='demo ficticia; no mercado real'
                    engine.apply_signal(Signal('DEMO-BUY','inversor-demo','DEMO','BUY',Decimal('1000'),stamp(t-timedelta(seconds=30)),a,src),PriceQuote('DEMO',Decimal('10'),a,src))
                    engine.apply_signal(Signal('DEMO-SELL','inversor-demo','DEMO','SELL',Decimal('1000'),stamp(t-timedelta(seconds=29)),b,src),PriceQuote('DEMO',Decimal('11'),b,src))
                    quotes['DEMO']={'price':'11','as_of':b,'source':src}; save(); self.reply({'ok':True,'state':state()}); return
                self.send_error(404)
        except (ValueError,KeyError,TypeError,EngineError) as e: self.reply({'ok':False,'error':{'code':getattr(e,'code','INVALID_REQUEST'),'detail':getattr(e,'detail',str(e))}},400)
        except Exception as e: print('Error interno:',repr(e)); self.reply({'ok':False,'error':{'code':'INTERNAL_ERROR','detail':'Error interno del servidor'}},500)


def main():
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", sys.argv[1] if len(sys.argv) > 1 else "8765"))

    fomo_feed.start()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Fomo Paper escuchando en {host}:{port}")
    print("Paper trading; las señales FOMO se copian solo a la cuenta virtual.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

