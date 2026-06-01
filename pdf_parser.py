"""
PDF 单据解析引擎 v3
支持: Packing List (2种布局), Evergreen BL, MSC BL, 通用格式
全部策略并行运行, 取结果最多的
"""
import fitz, re
from collections import OrderedDict
from typing import Dict, List, Callable


def parse_pdf(filepath: str) -> Dict:
    doc = fitz.open(filepath)
    text = ""
    for page in doc:
        text += page.get_text()
    doc.close()

    result = {
        'filename': filepath.split('/')[-1],
        'doc_refs': [],
        'bl_nos': [],
        'total_weight': '',
        'total_containers': '',
        'containers': OrderedDict(),
    }

    result['doc_refs'] = list(set(re.findall(r'CS-\d+[A-Za-z]?', text)))

    for pat in [r'(?:B/L|Bill\s*of\s*Lading)\s*(?:NO\.?|No\.?|Number)?[:\s]*([A-Z]{4}\d{9,12})',
                 r'B/L\s*NO\.?\s*([A-Z]{4}\d{9,12})']:
        found = re.findall(pat, text, re.IGNORECASE)
        result['bl_nos'].extend(found)
    result['bl_nos'] = list(set(result['bl_nos']))

    total_m = re.search(r'TOTAL[:\s]*([\d,.]+)\s*(?:MTS?|MT|KGS?)?', text, re.IGNORECASE)
    if total_m:
        result['total_weight'] = total_m.group(1)

    cnt_m = re.search(r'TOTAL\s*(?:CONTAINERS?|PACKAGES?)[:\s]*(\d+)', text, re.IGNORECASE)
    if cnt_m:
        result['total_containers'] = cnt_m.group(1)

    lines = text.split('\n')

    # 全部策略并行, 取结果最多的
    strategies = [parse_evergreen_bl, parse_msc_bl, parse_maersk_bl, parse_pl_v1, parse_pl_v2, parse_generic]
    best = OrderedDict()
    for fn in strategies:
        containers = OrderedDict()
        fn(lines, containers)
        if len(containers) > len(best):
            best = containers

    result['containers'] = best
    return result


def parse_pl_v1(lines: List[str], out: Dict):
    """Layout A: Container / Seal / Weight 逐行"""
    for i, line in enumerate(lines):
        m = re.match(r'^([A-Z]{4}\d{7})$', line.strip())
        if not m: continue
        cntr = m.group(1)
        seals = []
        if i + 1 < len(lines):
            seals = re.findall(r'(\d{7})', lines[i + 1])[:2]
        wt = ""
        if i + 2 < len(lines):
            wts = re.findall(r'(\d+\.\d+)', lines[i + 2])
            if wts: wt = wts[0]
        if len(seals) >= 2:
            out[cntr] = {'seal1': seals[0], 'seal2': seals[1], 'weight': wt}


def parse_pl_v2(lines: List[str], out: Dict):
    """
    Layout B: Container + Seal 在不同区域 (多列表格)
    集装箱号单独一行, 封号在文档后半部分
    策略: 收集所有集装箱号 + 所有封号行 → 按索引配对
    """
    # 1. 找所有集装箱号 (纯4字母+7数字, 独占一行)
    cntr_entries = []
    for i, line in enumerate(lines):
        m = re.match(r'^([A-Z]{4}\d{7})$', line.strip())
        if m:
            cntr_entries.append((i, m.group(1)))
    if len(cntr_entries) < 2: return

    # 2. 找所有封号行 (包含7-8位数字, 可能在Seal No表头之后, 或堆积在文档后部)
    seal_lines = []
    in_seal_area = False
    seal_header_seen = False

    for i, line in enumerate(lines):
        s = line.strip()
        # "Seal No" 表头标记
        if re.match(r'^Seal\s*No', s):
            seal_header_seen = True
            continue
        # 含封号的行
        digits = re.findall(r'\b(\d{7,8})\b', s)
        if not digits: continue
        # 过滤掉重量行 (有小数点) 和非封号数字
        if re.search(r'\d+\.\d+', s): continue
        # 排除明显不是封号的行 (如日期、电话等)
        if re.search(r'(?:Date|TEL|FAX|Page|B/L|KGS|MTS)', s, re.IGNORECASE): continue

        # "Seal No" 之后的所有含封号行
        if seal_header_seen:
            seal_lines.append(digits)
        # 或者在集装箱区域之后堆积的封号 (最后一个柜之后)
        elif cntr_entries and i > cntr_entries[-1][0]:
            seal_lines.append(digits)

    # 如果通过Seal No表头没找到, 尝试: 找连续的封号行 (至少3行连续都是纯封号)
    if not seal_lines:
        seal_candidates = []
        for i, line in enumerate(lines):
            s = line.strip()
            digits = re.findall(r'\b(\d{7,8})\b', s)
            if digits and not re.search(r'[A-Za-z.]', s):
                seal_candidates.append((i, digits))
        # 找最大连续块
        if seal_candidates:
            best_block = []
            current_block = [seal_candidates[0]]
            for j in range(1, len(seal_candidates)):
                if seal_candidates[j][0] - seal_candidates[j-1][0] <= 2:
                    current_block.append(seal_candidates[j])
                else:
                    if len(current_block) > len(best_block):
                        best_block = current_block
                    current_block = [seal_candidates[j]]
            if len(current_block) > len(best_block):
                best_block = current_block
            seal_lines = [d for _, d in best_block]

    if not seal_lines: return

    # 3. 按索引配对
    n = min(len(cntr_entries), len(seal_lines))
    for idx in range(n):
        pos, cntr = cntr_entries[idx]
        seals = seal_lines[idx]
        # 重量: 集装箱下面第一行
        wt = ""
        if pos + 1 < len(lines):
            wts = re.findall(r'^(\d+\.\d+)$', lines[pos + 1].strip())
            if wts: wt = wts[0]
        out[cntr] = {
            'seal1': seals[0],
            'seal2': seals[1] if len(seals) > 1 else seals[0],
            'weight': wt,
        }


def parse_evergreen_bl(lines: List[str], out: Dict):
    """Evergreen BL: CONTAINER/SIZE/SEAL1"""
    for i, line in enumerate(lines):
        m = re.match(r'^([A-Z]{4}\d{7})/\d{2,3}\'/(\d{7})\s+/', line.strip())
        if not m: continue
        cntr = m.group(1); seal1 = m.group(2)
        seal2 = ''
        if i + 1 < len(lines):
            m2 = re.match(r'\s*/(\d{7})\s*/', lines[i + 1])
            if m2: seal2 = m2.group(1)
        wt_m = re.search(r'([\d.]+)\s+KGS', line)
        out[cntr] = {'seal1': seal1, 'seal2': seal2, 'weight': wt_m.group(1) if wt_m else ''}


def parse_msc_bl(lines: List[str], out: Dict):
    """MSC BL: 集装箱号+Seal Number(s)"""
    cntr_positions = [(i, re.match(r'^([A-Z]{4}\d{7})$', line.strip()).group(1))
                      for i, line in enumerate(lines)
                      if re.match(r'^([A-Z]{4}\d{7})$', line.strip())]
    for pos, cntr in cntr_positions:
        seals = []
        for j in range(pos + 1, min(pos + 10, len(lines))):
            seals.extend(re.findall(r'FX(\d{7,8})', lines[j]))
        if len(seals) >= 2:
            out[cntr] = {'seal1': seals[0], 'seal2': seals[1], 'weight': ''}


def parse_maersk_bl(lines: List[str], out: Dict):
    """
    Maersk BL: 集装箱号在行首, Seal在下一行
    HASU1250356  20 DRY 8'6  1 BULK  22180.000 KGS  20.0000 CBM
    Shipper Seal :  1454790 1454789
    """
    for i, line in enumerate(lines):
        # Container + weight on same line
        m = re.match(r'^([A-Z]{4}\d{7})\s+\d+\s+DRY', line.strip())
        if not m: continue
        cntr = m.group(1)
        # Weight from KGS
        wt_m = re.search(r'([\d.]+)\s+KGS', line)
        wt = wt_m.group(1) if wt_m else ''
        # Seal on next line
        seals = []
        if i + 1 < len(lines):
            sm = re.search(r'Shipper\s*Seal\s*:\s*([\d\s]+)', lines[i+1], re.IGNORECASE)
            if sm:
                seals = re.findall(r'(\d{7,8})', sm.group(1))
        if i + 2 < len(lines) and not seals:
            sm = re.search(r'Shipper\s*Seal\s*:\s*([\d\s]+)', lines[i+2], re.IGNORECASE)
            if sm:
                seals = re.findall(r'(\d{7,8})', sm.group(1))

        if seals:
            out[cntr] = {
                'seal1': seals[0],
                'seal2': seals[1] if len(seals) > 1 else seals[0],  # 单封号时两个都填同一个
                'weight': wt,
            }


def parse_generic(lines: List[str], out: Dict):
    """通用: 集装箱号+附近数字"""
    for i, line in enumerate(lines):
        m = re.match(r'^([A-Z]{4}\d{7})$', line.strip())
        if not m: continue
        seal_nums = []
        for j in range(i + 1, min(i + 6, len(lines))):
            # Also check for "Shipper Seal" pattern
            sm = re.search(r'(?:Shipper\s*)?Seal\s*:?\s*([\d\s]+)', lines[j], re.IGNORECASE)
            if sm:
                seal_nums.extend(re.findall(r'(\d{7,8})', sm.group(1)))
            else:
                seal_nums.extend(re.findall(r'\b(\d{7,8})\b', lines[j]))
        if len(seal_nums) >= 1:
            out[m.group(1)] = {
                'seal1': seal_nums[0],
                'seal2': seal_nums[1] if len(seal_nums) > 1 else seal_nums[0],
                'weight': '',
            }
