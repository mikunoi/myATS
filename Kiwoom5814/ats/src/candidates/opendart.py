"""Small OpenDART client used by the weekly research job."""
from datetime import date, timedelta
from html import unescape
import io
import json
import re
from urllib.parse import urlencode
from urllib.request import urlopen
from zipfile import ZipFile
from xml.etree import ElementTree


class OpenDartError(RuntimeError):
    pass


class OpenDartClient:
    BASE = "https://opendart.fss.or.kr/api"

    def __init__(self, api_key, opener=urlopen):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("OpenDART API key is required")
        self.api_key, self.opener = api_key.strip(), opener

    def _get(self, endpoint, **params):
        query = urlencode({"crtfc_key": self.api_key, **params})
        with self.opener(f"{self.BASE}/{endpoint}?{query}", timeout=20) as response:
            return response.read()

    def _json(self, endpoint, **params):
        result = json.loads(self._get(endpoint, **params).decode("utf-8"))
        status = str(result.get("status", ""))
        if status not in {"000", "013"}:  # 013 means no matching data.
            raise OpenDartError(f"OpenDART {endpoint} failed: status={status}")
        return result

    def listed_corporations(self):
        raw = self._get("corpCode.xml")
        try:
            with ZipFile(io.BytesIO(raw)) as archive:
                xml = archive.read(archive.namelist()[0])
        except Exception as error:
            raise OpenDartError("Invalid OpenDART corporation-code archive") from error
        root, result = ElementTree.fromstring(xml), {}
        for row in root.findall("list"):
            stock = (row.findtext("stock_code") or "").strip()
            corp = (row.findtext("corp_code") or "").strip()
            if len(stock) == 6 and len(corp) == 8:
                result[stock] = {"corp_code": corp,
                                 "corp_name": (row.findtext("corp_name") or "").strip()}
        return result

    @staticmethod
    def _amount(row):
        value = str(row.get("thstrm_amount", "")).replace(",", "").strip()
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    @classmethod
    def _cashflow(cls, rows):
        operating, capex = None, []
        for row in rows:
            if row.get("sj_div") != "CF":
                continue
            name = str(row.get("account_nm", "")).replace(" ", "")
            account_id = str(row.get("account_id", ""))
            amount = cls._amount(row)
            if amount is None:
                continue
            if ("CashFlowsFromUsedInOperatingActivities" in account_id
                    or name in {"영업활동현금흐름", "영업활동으로인한현금흐름"}):
                operating = amount
            if ("PurchaseOfPropertyPlantAndEquipment" in account_id
                    or "유형자산의취득" in name):
                capex.append(abs(amount))
        return operating, sum(capex) if capex else None

    def annual_fcf(self, corp_code, year):
        for division in ("CFS", "OFS"):
            result = self._json("fnlttSinglAcntAll.json", corp_code=corp_code,
                                bsns_year=str(year), reprt_code="11011", fs_div=division)
            operating, capex = self._cashflow(result.get("list", []))
            if operating is not None and capex is not None:
                return {"year": year, "fs_div": division, "operating_cash_flow": operating,
                        "capex": capex, "fcf": operating - capex}
        return None

    def disclosures(self, corp_code, as_of=None, days=365):
        end = as_of or date.today()
        result = self._json("list.json", corp_code=corp_code,
                            bgn_de=(end - timedelta(days=days)).strftime("%Y%m%d"),
                            end_de=end.strftime("%Y%m%d"), last_reprt_at="Y",
                            sort="date", sort_mth="desc", page_count="100")
        return [{key: row.get(key, "") for key in
                 ("rcept_no", "report_nm", "rcept_dt", "pblntf_ty")}
                for row in result.get("list", [])]

    def company(self, corp_code):
        result = self._json("company.json", corp_code=corp_code)
        return {key: result.get(key, "") for key in
                ("corp_name", "corp_name_eng", "stock_name", "stock_code",
                 "corp_cls", "induty_code", "est_dt", "acc_mt", "hm_url", "ir_url")}

    def document_text(self, receipt_number):
        raw = self._get("document.xml", rcept_no=receipt_number)
        try:
            with ZipFile(io.BytesIO(raw)) as archive:
                parts = []
                for name in archive.namelist():
                    payload = archive.read(name)
                    text = payload.decode("utf-8", errors="replace")
                    if text.count("�") > max(5, len(text) // 100):
                        text = payload.decode("euc-kr", errors="replace")
                    parts.append(text)
        except Exception as error:
            raise OpenDartError("Invalid OpenDART disclosure-document archive") from error
        plain = re.sub(r"<[^>]+>", " ", "\n".join(parts))
        return re.sub(r"\s+", " ", unescape(plain)).strip()

    @staticmethod
    def business_sections(text, maximum=2500):
        """Extract bounded evidence around the business and product headings."""
        sections = {}
        headings = {
            "business_overview": ("사업의 개요", "사업 개요", "산업의 특성", "산업의 개요"),
            "products_services": ("주요 제품 및 서비스", "주요 제품", "주요 상품 및 서비스"),
        }
        for key, candidates in headings.items():
            positions = [(text.find(label), label) for label in candidates if text.find(label) >= 0]
            if positions:
                start, label = min(positions)
                sections[key] = {"heading": label, "text": text[start:start + maximum]}
        return sections
