"""Small, persistent EUR paper-trading ledger used by Fomo Paper."""
from __future__ import annotations
import hashlib, json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path

class EngineError(ValueError):
    code = "ENGINE_ERROR"
    detail = ""
    def __init__(self, detail): self.detail = detail; super().__init__(detail)

@dataclass
class Config:
    commission_rate: Decimal = Decimal("0.001")
    commission_min: Decimal = Decimal("0.00")
    commission_fixed: Decimal = Decimal("0.00")
    slippage_bps: Decimal = Decimal("10")
    max_delay_min: Decimal = Decimal("30")
    copy_allocation_pct: Decimal = Decimal("0.10")
    base_currency: str = "EUR"

@dataclass
class Instrument:
    asset: str
    currency: str

@dataclass
class PriceQuote:
    asset: str
    price: Decimal
    as_of: str
    source: str

@dataclass
class FxQuote:
    currency: str
    rate: Decimal
    as_of: str
    source: str

@dataclass
class Signal:
    signal_id: str
    investor_id: str
    asset: str
    direction: str
    observed_size: Decimal
    original_at: str
    detected_at: str
    source: str
    status: str = "OK"

@dataclass
class SignalRecord:
    state: str
    reason_code: str
    reason_text: str

def _d(x): return Decimal(str(x))
def _iso(s): return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
def _s(x): return format(x, "f")

class Engine:
    def __init__(self, config=None):
        self.config = config or Config()
        self.initial_cash = Decimal("100.00")
        self.cash = self.initial_cash
        self.instruments = {}
        self.signals = []
        self.copies = {}
        self.fills = []
        self.equity_history = []
        self._history_point("Inicio", "100.00", "")

    def register_instrument(self, instrument): self.instruments[instrument.asset] = instrument

    def _fee(self, amount):
        c=self.config
        return max(c.commission_min, amount*c.commission_rate)+c.commission_fixed

    def _price_eur(self, asset, price, fx=None):
        instrument=self.instruments.get(asset)
        if instrument and instrument.currency=="USD":
            if not fx: raise EngineError("Falta la referencia USD/EUR para valorar esta señal")
            return _d(price)*_d(fx.rate)
        return _d(price)

    def _equity(self):
        return self.cash + sum((c["quantity"]*c["entry_price"] for c in self.copies.values() if c["status"]=="OPEN"), Decimal("0"))

    def _history_point(self, label, equity, at):
        self.equity_history.append({"label": label, "equity_eur": str(equity), "at": at})
        self.equity_history=self.equity_history[-501:]

    def apply_signal(self, signal, quote, fx=None):
        try: delay=(_iso(signal.detected_at)-_iso(signal.original_at)).total_seconds()/60
        except Exception: delay=0
        record={"signal_id":signal.signal_id,"investor_id":signal.investor_id,"asset":signal.asset,"direction":signal.direction,"observed_size":_s(signal.observed_size),"original_at":signal.original_at,"detected_at":signal.detected_at,"source":signal.source,"delay_min":round(delay,3),"state":"EJECUTADA","reason_code":"OK","execution_price_eur":None}
        if delay>float(self.config.max_delay_min):
            record.update(state="RECHAZADA",reason_code="DELAY_EXCEEDED",reason_text="La señal llegó fuera del retraso máximo")
            self.signals.append(record); return SignalRecord(record["state"],record["reason_code"],record["reason_text"])
        px=self._price_eur(signal.asset,quote.price,fx)
        if px<=0: raise EngineError("El precio debe ser positivo")
        if signal.direction=="BUY":
            budget=self.cash*self.config.copy_allocation_pct
            if budget<=0 or self.cash<=0:
                record.update(state="RECHAZADA",reason_code="NO_CASH",reason_text="No hay efectivo virtual disponible")
            else:
                gross=budget/(Decimal("1")+self.config.commission_rate)
                fee=self._fee(gross); gross=min(gross,self.cash-fee)
                execution=px*(Decimal("1")+self.config.slippage_bps/Decimal("10000"))
                qty=(gross/execution).quantize(Decimal("0.000000000001"),rounding=ROUND_DOWN)
                cost=qty*execution; total=cost+fee
                if qty<=0 or total>self.cash:
                    record.update(state="RECHAZADA",reason_code="NO_CASH",reason_text="Efectivo insuficiente para simular la compra")
                else:
                    cid="COPY-"+signal.signal_id
                    self.cash-=total
                    self.copies[cid]={"copy_id":cid,"signal_id":signal.signal_id,"investor_id":signal.investor_id,"asset":signal.asset,"quantity":qty,"cost_eur":total,"entry_price":execution,"opened_at":signal.detected_at,"status":"OPEN"}
                    self.fills.append({"copy_id":cid,"signal_id":signal.signal_id,"side":"BUY","quantity":_s(qty),"price":_s(execution),"fee_eur":_s(fee),"at":signal.detected_at})
                    record["execution_price_eur"]=_s(execution)
        else:
            matching=[(cid,c) for cid,c in self.copies.items() if c["status"]=="OPEN" and c["asset"]==signal.asset and c["investor_id"]==signal.investor_id]
            if not matching:
                record.update(state="RECHAZADA",reason_code="NO_OPEN_COPY",reason_text="No hay una copia abierta de este activo para cerrar")
            else:
                execution=px*(Decimal("1")-self.config.slippage_bps/Decimal("10000")); fee_total=Decimal("0")
                for cid,c in matching:
                    proceeds=c["quantity"]*execution; fee=self._fee(proceeds); self.cash+=proceeds-fee; fee_total+=fee
                    pnl=proceeds-fee-c["cost_eur"]
                    c.update(status="CLOSED",closed_at=signal.detected_at,exit_price=execution,proceeds_eur=proceeds-fee,realized_pnl_eur=pnl)
                    self.fills.append({"copy_id":cid,"signal_id":signal.signal_id,"side":"SELL","quantity":_s(c["quantity"]),"price":_s(execution),"fee_eur":_s(fee),"at":signal.detected_at})
                record["execution_price_eur"]=_s(execution)
        self.signals.append(record)
        self._history_point(signal.direction+" "+signal.asset,_s(self._equity()),signal.detected_at)
        return SignalRecord(record["state"],record["reason_code"],record.get("reason_text",""))

    def close_copy(self, copy_id, quote, at=None, fx=None):
        c=self.copies.get(copy_id)
        if not c or c["status"]!="OPEN": raise EngineError("Posición no encontrada o ya cerrada")
        px=self._price_eur(c["asset"],quote.price,fx)*(Decimal("1")-self.config.slippage_bps/Decimal("10000")); proceeds=c["quantity"]*px; fee=self._fee(proceeds)
        self.cash+=proceeds-fee; pnl=proceeds-fee-c["cost_eur"]
        c.update(status="CLOSED",closed_at=at or quote.as_of,exit_price=px,proceeds_eur=proceeds-fee,realized_pnl_eur=pnl)
        self.fills.append({"copy_id":copy_id,"side":"SELL","quantity":_s(c["quantity"]),"price":_s(px),"fee_eur":_s(fee),"at":at or quote.as_of})
        self._history_point("Cierre "+c["asset"],_s(self._equity()),at or quote.as_of)

    def snapshot(self, marks, as_of):
        opens=[]; closed=[]; realized=Decimal("0"); unrealized=Decimal("0"); value=Decimal("0")
        for c in self.copies.values():
            base={k:c[k] for k in ("copy_id","signal_id","investor_id","asset","opened_at")}
            base.update(quantity=_s(c["quantity"]),cost_eur=_s(c["cost_eur"]))
            if c["status"]=="OPEN":
                mark=marks.get(c["asset"]); px=self._price_eur(c["asset"],mark[0].price,mark[1]) if mark else c["entry_price"]; mv=c["quantity"]*px; upnl=mv-c["cost_eur"]
                value+=mv; unrealized+=upnl
                base.update(market_price_eur=_s(px),market_value_eur=_s(mv),unrealized_pnl_eur=_s(upnl)); opens.append(base)
            else:
                realized+=c["realized_pnl_eur"]; base.update(closed_at=c["closed_at"],realized_pnl_eur=_s(c["realized_pnl_eur"])); closed.append(base)
        equity=self.cash+value
        return {"as_of":as_of,"account":{"initial_cash_eur":_s(self.initial_cash),"cash_eur":_s(self.cash),"equity_eur":_s(equity),"realized_pnl_eur":_s(realized),"unrealized_pnl_eur":_s(unrealized)},"config":{"commission_rate":_s(self.config.commission_rate),"slippage_bps":_s(self.config.slippage_bps),"max_delay_min":_s(self.config.max_delay_min),"copy_allocation_pct":_s(self.config.copy_allocation_pct)},"signals":self.signals[-500:],"open_copies":opens,"closed_copies":closed,"equity_history":self.equity_history}

    def ledger_digest(self): return hashlib.sha256(json.dumps(self.fills,sort_keys=True,separators=(",",":")).encode()).hexdigest()
    def save(self,path):
        Path(path).write_text(json.dumps({"version":1,"config":{k:_s(v) if isinstance(v,Decimal) else v for k,v in vars(self.config).items()},"initial_cash":_s(self.initial_cash),"cash":_s(self.cash),"instruments":[vars(v) for v in self.instruments.values()],"signals":self.signals,"copies":{cid:{k:_s(v) if isinstance(v,Decimal) else v for k,v in c.items()} for cid,c in self.copies.items()},"fills":self.fills,"equity_history":self.equity_history},indent=2),encoding="utf-8")
    @classmethod
    def load(cls,path):
        raw=json.loads(Path(path).read_text(encoding="utf-8")); cfg=Config(**{k:(_d(v) if k!="base_currency" else v) for k,v in raw.get("config",{}).items()}); obj=cls(cfg)
        obj.initial_cash=_d(raw.get("initial_cash",100)); obj.cash=_d(raw.get("cash",obj.initial_cash)); obj.instruments={x["asset"]:Instrument(**x) for x in raw.get("instruments",[])}
        obj.signals=raw.get("signals",[]); obj.fills=raw.get("fills",[]); obj.equity_history=raw.get("equity_history",obj.equity_history)
        stored_copies=raw.get("copies",{})
        if isinstance(stored_copies,list): stored_copies={}
        for cid,c in stored_copies.items():
            for k in ("quantity","cost_eur","entry_price","exit_price","proceeds_eur","realized_pnl_eur"):
                if k in c:c[k]=_d(c[k])
            obj.copies[cid]=c
        return obj
