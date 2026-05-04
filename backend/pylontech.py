"""Pylontech US2000B RS232 via Elfin EE10 TCP (identique à la version précédente)."""
import asyncio, logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

CMD_PWR = b"pwr\r\n"
CONNECT_TIMEOUT  = 5.0
RESPONSE_TIMEOUT = 12.0
COL_ID,COL_VOLT,COL_CURR=0,1,2
COL_TEMPR,COL_TLOW,COL_THIGH=3,4,5
COL_VLOW,COL_VHIGH=6,7
COL_BASE_ST,COL_VOLT_ST,COL_CURR_ST,COL_TEMP_ST,COL_COULOMB=8,9,10,11,12
MIN_COLS=12
_SKIP={"power","pwr","stat","soc","@@","@"}

@dataclass
class PylontechBattery:
    battery_id:int=0
    voltage:Optional[float]=None
    current:Optional[float]=None
    temperature:Optional[float]=None
    temperature_low:Optional[float]=None
    temperature_high:Optional[float]=None
    voltage_low:Optional[float]=None
    voltage_high:Optional[float]=None
    coulomb:Optional[float]=None
    soc_percent:Optional[float]=None
    mos_temperature:Optional[float]=None
    base_state:Optional[str]=None
    voltage_state:Optional[str]=None
    current_state:Optional[str]=None
    temperature_state:Optional[str]=None
    discharge_capacity:Optional[float]=None
    cycle_count:Optional[int]=None

def _f(s):
    s=s.strip()
    return None if s in("-","--","") else (float(s) if s.replace("-","").replace(".","").isdigit() else None)
def _pct(s): return _f(s.strip().rstrip("%"))
def _mv(v): return round(v/1000.0,3) if v is not None else None
def _ma(v): return round(v/1000.0,3) if v is not None else None
def _mc(v): return round(v/1000.0,3) if v is not None else None
def _skip(l): return not l or (l.split()[0].lower() in _SKIP if l.split() else True)

def parse_pwr(raw):
    result=[]
    for line in raw.splitlines():
        line=line.strip()
        if _skip(line): continue
        p=line.split()
        try: bat_id=int(p[0])
        except: continue
        bat=PylontechBattery(battery_id=bat_id)
        if len(p)>COL_BASE_ST and p[COL_BASE_ST].lower()=="absent":
            bat.base_state="Absent"; result.append(bat); continue
        if len(p)<MIN_COLS: continue
        bat.voltage=_mv(_f(p[COL_VOLT])); bat.current=_ma(_f(p[COL_CURR]))
        bat.temperature=_mc(_f(p[COL_TEMPR])); bat.temperature_low=_mc(_f(p[COL_TLOW]))
        bat.temperature_high=_mc(_f(p[COL_THIGH])); bat.voltage_low=_mv(_f(p[COL_VLOW]))
        bat.voltage_high=_mv(_f(p[COL_VHIGH])); bat.base_state=p[COL_BASE_ST]
        bat.voltage_state=p[COL_VOLT_ST] if len(p)>COL_VOLT_ST else None
        bat.current_state=p[COL_CURR_ST] if len(p)>COL_CURR_ST else None
        bat.temperature_state=p[COL_TEMP_ST] if len(p)>COL_TEMP_ST else None
        if len(p)>COL_COULOMB: bat.coulomb=bat.soc_percent=_pct(p[COL_COULOMB])
        result.append(bat)
    return result

def parse_stat(raw,bat):
    for line in raw.splitlines():
        if ":"not in line: continue
        key,_,val=line.partition(":"); key,val=key.strip().lower(),val.strip()
        if "pwr percent"in key:
            v=_f(val)
            if v is not None: bat.soc_percent=v; bat.coulomb=bat.coulomb or v
        elif "dsg cap"in key:
            v=_f(val)
            if v is not None: bat.discharge_capacity=round(v/1000.0,1)
        elif "cycle"in key:
            try: bat.cycle_count=int(float(val))
            except: pass

class PylontechPoller:
    def __init__(self,host,port=9999,num_batteries=4):
        self.host,self.port,self.num_batteries=host,port,num_batteries
    async def _connect(self):
        return await asyncio.wait_for(asyncio.open_connection(self.host,self.port),timeout=CONNECT_TIMEOUT)
    async def _read(self,reader):
        parts,deadline=[],asyncio.get_event_loop().time()+RESPONSE_TIMEOUT
        while True:
            rem=deadline-asyncio.get_event_loop().time()
            if rem<=0: break
            try:
                chunk=await asyncio.wait_for(reader.read(4096),timeout=min(rem,2.0))
                if not chunk: break
                parts.append(chunk.decode("ascii",errors="replace"))
                full="".join(parts)
                if "$$"in full or "successfully"in full.lower():
                    try:
                        ex=await asyncio.wait_for(reader.read(4096),timeout=0.5)
                        if ex: parts.append(ex.decode("ascii",errors="replace"))
                    except asyncio.TimeoutError: pass
                    break
            except asyncio.TimeoutError: break
        return "".join(parts)
    async def _send(self,writer,reader,cmd):
        try: await asyncio.wait_for(reader.read(4096),timeout=0.3)
        except asyncio.TimeoutError: pass
        writer.write(cmd); await writer.drain()
        return await self._read(reader)
    async def poll(self):
        reader,writer=await self._connect()
        try:
            writer.write(b"\r\n"); await writer.drain(); await asyncio.sleep(0.3)
            batteries=parse_pwr(await self._send(writer,reader,CMD_PWR))
            for bat in batteries:
                if bat.base_state=="Absent": continue
                try:
                    raw_s=await self._send(writer,reader,f"stat {bat.battery_id}\r\n".encode())
                    parse_stat(raw_s,bat)
                except Exception as e: logger.debug("STAT %d: %s",bat.battery_id,e)
            return batteries
        finally:
            try: writer.close(); await writer.wait_closed()
            except: pass
    def to_dict(self,bat):
        soc=bat.soc_percent if bat.soc_percent is not None else bat.coulomb
        return {
            "id":bat.battery_id,"type":"pylontech",
            "voltage":bat.voltage,"current":bat.current,
            "power":round(bat.voltage*bat.current,1) if bat.voltage and bat.current else None,
            "soc":soc,"temperature":bat.temperature,
            "temperature_low":bat.temperature_low,"temperature_high":bat.temperature_high,
            "voltage_low":bat.voltage_low,"voltage_high":bat.voltage_high,
            "mos_temperature":bat.mos_temperature,
            "base_state":bat.base_state,"voltage_state":bat.voltage_state,
            "current_state":bat.current_state,"temperature_state":bat.temperature_state,
            "discharge_capacity":bat.discharge_capacity,"cycle_count":bat.cycle_count,
            "cells":{},"alarms":[],"alarm_count":0,"online":True,
        }
