from src.reporting.notion_manual_holdings import NotionManualHoldingsSync
"""Event-driven Notion synchronization for Kiwoom US holdings."""
import json,time
from decimal import Decimal
from src.reporting.notion_api import NotionError,NotionTransport
from src.config import load_settings, Settings

class Notion5814USSync:
 def __init__(self,root,token,store,logger,names,transport=None,retry_seconds=60):
  self.store,self.logger,self.names=store,logger,names;self.transport=transport or NotionTransport(token)
  self.config=json.loads((root/"config/notion_5814.mapping.json").read_text(encoding="utf-8"));self.configured=bool(token) and self.config.get("enabled") is True
  self.retry_seconds,self.last_attempt=retry_seconds,0.0
  self.manual_holdings=NotionManualHoldingsSync(self.transport,self.config,logger)
  self.addon_multiple=(load_settings(root) if (root / "config/default.json").exists() else Settings()).addon_atr_multiple
 @staticmethod
 def num(v):return {"number":float(Decimal(str(v)))}
 @staticmethod
 def select(v):return {"select":{"name":v}}
 @staticmethod
 def status(v):return {"status":{"name":v}}
 @staticmethod
 def title(v):return {"title":[{"type":"text","text":{"content":v}}]}
 @staticmethod
 def text(v):return {"rich_text":[{"type":"text","text":{"content":v}}]}
 @staticmethod
 def date(v):return {"date":{"start":v}}
 def page(self,s):return self.store.system_value("notion_5814_us_page:"+s,"")
 def props(self,p,new=False):
  x={"계좌":self.select("키움"),"통화":self.select("USD"),"상태":self.status("매수"),"리스크":self.select("0.5%"),"포지션":self.select("일봉" if p["mode"]=="DAILY" else "주봉"),"평단":self.num(p["average_price_krw"]),"보유수량":self.num(p["quantity"]),"최근 진입가":self.num(p["latest_entry_price"]),"최근 n값":self.num(p["atr"]),"유닛수":self.num(p["units"]),"계약수":self.num(p["contracts"]),"최근 체결일자":self.date(p["date"])}
  x["추매 ATR 배수"]=self.num(self.addon_multiple)
  x["종목"]=self.title(p["symbol"].upper())
  if new:x.update({"비고":self.text("자동매매 거래ID:"+p["order_id"])})
  return x
 def apply(self,p):
  s=p["symbol"];page=p.get("_target_page") or self.page(s)
  if p["side"]=="SELL":
   if not page:raise NotionError("US holding page mapping is missing")
   if p.get("profit_pending"):
    if not p.get("_status_synced"):
     self.transport.request("PATCH","/v1/pages/"+page,{"properties":{"상태":self.status("종료"),"최근 체결일자":self.date(p["date"])}})
    return False
   self.transport.request("PATCH","/v1/pages/"+page,{"properties":{"상태":self.status("종료"),"실현손익":self.num(p["profit_amount"]),"손익률":self.num(p["profit_rate"]),"최근 체결일자":self.date(p["date"])}});return True
  if p["side"]=="SYNC":
   if not page:return False
   self.transport.request("PATCH","/v1/pages/"+page,{"properties":{"평단":self.num(p["average_price_krw"]),"보유수량":self.num(p["quantity"]),"최근 체결일자":self.date(p["date"])}});return True
  if not page or p.get("new_cycle"):
   marker="자동매매 거래ID:"+p["order_id"]
   found=self.transport.request("POST","/v1/data_sources/"+self.config["data_source_id"]+"/query",{"page_size":2,"filter":{"property":"비고","rich_text":{"equals":marker}}}).get("results",[])
   if len(found)>1:raise NotionError("Duplicate US trade pages found")
   page=found[0].get("id") if found else None
   if not page:
    r=self.transport.request("POST","/v1/pages",{"parent":{"type":"data_source_id","data_source_id":self.config["data_source_id"]},"properties":self.props(p,True)});page=r.get("id")
   if not page:raise NotionError("US holding page identifier missing")
   self.store.set_system_value("notion_5814_us_page:"+s,page);return True
  self.transport.request("PATCH","/v1/pages/"+page,{"properties":self.props(p)});return True
 def sync_pending(self):
  if not self.configured or time.monotonic()-self.last_attempt<self.retry_seconds:return False
  self.last_attempt=time.monotonic();changed=False
  errors=[];blocked_symbols=set()
  for key,raw in self.store.events_by_category("notion_holding_update"):
   if self.store.system_value("notion_5814_us_applied:"+key,"")=="1":continue
   try:
    p=json.loads(raw)
    if p["symbol"] in blocked_symbols:continue
    if p.get("side")=="SELL":
     target_key="notion_5814_us_sell_page:"+key
     page=self.store.system_value(target_key,"") or self.page(p["symbol"])
     if not page:raise NotionError("US holding page mapping is missing")
     self.store.set_system_value(target_key,page);p["_target_page"]=page
     closed_key="notion_5814_us_closed:"+key
     p["_status_synced"]=self.store.system_value(closed_key,"")=="1"
    if self.apply(p):self.store.set_system_value("notion_5814_us_applied:"+key,"1");changed=True
    elif p.get("side")=="SELL":
     changed|=not p["_status_synced"];self.store.set_system_value(closed_key,"1")
   except (NotionError,KeyError,TypeError,ValueError,OSError) as e:
    try:blocked_symbols.add(json.loads(raw)["symbol"])
    except (KeyError,TypeError,ValueError):pass
    errors.append(type(e).__name__);self.logger.error("Notion US synchronization failed for %s: %s",key,type(e).__name__)
  self.store.set_system_value("notion_5814_us_last_error",",".join(errors));return changed
