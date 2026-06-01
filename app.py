"""
单据比对工具 — 支持 2+ PDF/BL/PackingList 对比
====================================================
用法: streamlit run doctool/app.py
"""
import streamlit as st
import pandas as pd
import tempfile, os, io, hashlib, hmac, time
from collections import defaultdict

# Add doctool to path
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pdf_parser import parse_pdf

# ============================================================
# 激活码系统
# ============================================================
SECRET = "doctool2026-v2"

def verify_key(key):
    try:
        parts = key.strip().split("-")
        if len(parts) != 3 or parts[0] != "DT": return False, 0
        expiry = int(parts[1])
        sig = parts[2]
        expected = hmac.new(SECRET.encode(), str(expiry).encode(), hashlib.sha256).hexdigest()[:8]
        if sig != expected: return False, 0
        remaining = (expiry - int(time.time())) / 86400
        return True, max(0, remaining)
    except:
        return False, 0

# Session state for activation
if 'activated' not in st.session_state:
    st.session_state.activated = False
    st.session_state.remaining_days = 0
    st.session_state.usage_today = 0

FREE_LIMIT = 20  # 免费每月比20次

st.set_page_config(page_title="单据比对工具", page_icon="📋", layout="wide")
st.title("📋 单据比对工具")
st.caption("上传 2 个或多个 PDF — 自动提取 Container No / Seal No / Weight 并比对差异")

# ============================================================
# Seal matching helper
# ============================================================
def seals_match(s1, s2):
    """
    跨格式封号比对: 处理FX前缀、7位vs8位等差异
    返回 True 如果封号等价
    """
    if not s1 or not s2:
        return s1 == s2  # both empty → match
    # Strip FX prefix
    a = s1.upper().replace('FX', '')
    b = s2.upper().replace('FX', '')
    # 前缀匹配 (PL 7位 == BL 8位去掉最后一位)
    return a == b or a.startswith(b) or b.startswith(a)

# ============================================================
# Sidebar
# ============================================================
with st.sidebar:
    st.header("⚙️ 设置")

    # 激活状态
    if st.session_state.activated:
        st.success(f"✅ 已激活 · 剩余 {st.session_state.remaining_days:.0f} 天")
        if st.button("🔓 退出登录"):
            st.session_state.activated = False
            st.rerun()
    else:
        with st.expander("🔑 激活码"):
            code = st.text_input("输入激活码", placeholder="DT-xxxxxxxxx-xxxxxxxx")
            if st.button("激活"):
                ok, days = verify_key(code)
                if ok:
                    st.session_state.activated = True
                    st.session_state.remaining_days = days
                    st.success(f"激活成功! 有效期 {days:.0f} 天")
                    st.rerun()
                else:
                    st.error("激活码无效或已过期")

    st.divider()
    st.caption("支持格式:")
    st.caption("• Packing List (表格/分列)")
    st.caption("• Evergreen BL")
    st.caption("• MSC BL")
    st.caption("• 通用PDF")
    st.divider()

    if not st.session_state.activated:
        st.caption("🔓 免费版: 每月20次比对")
        st.caption("🔐 专业版: 无限次数")
        st.caption("📱 加微信开通: 添哥")
    else:
        st.caption("🔐 专业版: 无限次数")

    st.divider()
    st.caption("封号自动匹配: FX前缀忽略, 7位/8位智能比对")

# ============================================================
# Upload
# ============================================================
files = st.file_uploader(
    "拖拽 PDF 文件到这里 (可多选, 2个或更多)",
    type=["pdf"],
    accept_multiple_files=True,
)

if not files:
    st.info("👆 上传 2 个或更多 PDF 文件开始比对")
    st.stop()

if len(files) < 2:
    st.warning("请上传至少 2 个文件")
    st.stop()

# 免费版限制
if not st.session_state.activated and st.session_state.usage_today >= FREE_LIMIT:
    st.warning(f"🔓 免费版每月 {FREE_LIMIT} 次，已达上限。输入激活码解锁无限使用。")
    st.stop()

# ============================================================
# Parse all files
# ============================================================
with st.spinner("解析 PDF..."):
    parsed = []
    for f in files:
        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp:
            tmp.write(f.read())
            tmp.flush()
            data = parse_pdf(tmp.name)
            data['display_name'] = f.name
            os.unlink(tmp.name)
        parsed.append(data)

# ============================================================
# Duplicate detection
# ============================================================
def find_duplicates(parsed):
    """
    检测重复:
    1. 同一文件内集装箱号重复
    2. 同一文件内封号重复
    3. 跨文件封号重复 (同一封号在不同集装箱上)
    返回 {container: [warnings]}
    """
    warnings = defaultdict(list)

    # 1. 跨文件: 同一封号出现在不同集装箱号上
    seal_to_containers = defaultdict(lambda: defaultdict(list))
    for p in parsed:
        for cntr, c in p['containers'].items():
            for seal_key in ['seal1', 'seal2']:
                s = c.get(seal_key, '')
                if s:
                    seal_to_containers[s][cntr].append(p['display_name'])

    for seal, cntr_map in seal_to_containers.items():
        unique_cntrs = list(cntr_map.keys())
        if len(unique_cntrs) > 1:
            for cntr in unique_cntrs:
                other = [c for c in unique_cntrs if c != cntr]
                files_involved = set()
                for o in other:
                    files_involved.update(cntr_map[o])
                warnings[cntr].append(
                    f"🔁 封号 {seal} 也出现在集装箱 {', '.join(other)} 上 (文件: {', '.join(sorted(files_involved))})"
                )

    # 2. 同一文件内: 封号重复
    for p in parsed:
        seal_positions = defaultdict(list)
        for cntr, c in p['containers'].items():
            for seal_key in ['seal1', 'seal2']:
                s = c.get(seal_key, '')
                if s:
                    seal_positions[s].append(cntr)
        for seal, cntrs in seal_positions.items():
            if len(cntrs) > 1:
                for cntr in cntrs:
                    warnings[cntr].append(
                        f"🔁 文件内封号 {seal} 重复 (出现 {len(cntrs)} 次: {', '.join(cntrs)})"
                    )

    return dict(warnings)

# Run duplicate detection
dup_warnings = find_duplicates(parsed)
if dup_warnings:
    n_dup = len(dup_warnings)
    st.warning(f"⚠️ 检测到 {n_dup} 个集装箱涉及重复封号/箱号问题，详见表格「备注」列")

# ============================================================
# Build comparison table
# ============================================================
# Collect all container numbers
all_keys = set()
for p in parsed:
    all_keys.update(p['containers'].keys())
all_keys = sorted(all_keys)

# Build rows
rows = []
n_mismatch = 0
n_match = 0

for key in all_keys:
    row = {'Container': key}
    seal_sets = []

    for p in parsed:
        name = p['display_name']
        c = p['containers'].get(key)
        if c:
            row[name] = f"{c['seal1']} / {c['seal2']}"
            w = c['weight']
            if w:
                row[f'{name} (重量)'] = w
            seal_sets.append((name, c['seal1'], c['seal2']))
        else:
            row[name] = '❌ 缺失'

    # Check seal consistency
    if len(seal_sets) > 1:
        ref = seal_sets[0]
        mismatch = False
        mismatch_details = []
        for name, s1, s2 in seal_sets[1:]:
            if not seals_match(ref[1], s1) or not seals_match(ref[2], s2):
                mismatch = True
                mismatch_details.append(f"{ref[0]} ↔ {name}: seal1={ref[1]} vs {s1}, seal2={ref[2]} vs {s2}")
        if mismatch:
            row['_mismatch'] = True
            row['_detail'] = '; '.join(mismatch_details)
            n_mismatch += 1
        else:
            row['_mismatch'] = False
            n_match += 1
    elif len(seal_sets) == 1:
        row['_mismatch'] = None  # only in one doc
    else:
        row['_mismatch'] = None

    rows.append(row)

# Count partial (only in some docs)
n_partial = sum(1 for r in rows if r['_mismatch'] is None)

# ============================================================
# Stats
# ============================================================
col1, col2, col3, col4 = st.columns(4)
with col1:
    st.metric("文件数", len(parsed))
with col2:
    st.metric("总柜数 (去重)", len(all_keys))
with col3:
    st.metric("完全匹配", n_match)
with col4:
    st.metric("有差异/缺失", n_mismatch + n_partial, delta_color="inverse")

# ============================================================
# File summary
# ============================================================
st.subheader("📄 文件概览")
info_cols = st.columns(len(parsed))
for idx, p in enumerate(parsed):
    with info_cols[idx]:
        st.markdown(f"**{p['display_name']}**")
        st.caption(f"柜数: {len(p['containers'])}")
        if p.get('doc_refs'):
            st.caption(f"编号: {', '.join(p['doc_refs'])}")
        if p.get('bl_nos'):
            st.caption(f"B/L: {', '.join(p['bl_nos'])}")
        if p.get('total_weight'):
            st.caption(f"总重: {p['total_weight']}")

# ============================================================
# Comparison Table
# ============================================================
st.subheader("🔍 比对结果")

# Build display dataframe
df_data = []
for r in rows:
    d = {'Container': r['Container']}
    for p in parsed:
        name = p['display_name']
        d[name] = r.get(name, '')
    # 状态
    if r['_mismatch'] is True:
        d['状态'] = '⚠️ 差异'
    elif r['_mismatch'] is None:
        d['状态'] = '❓ 单边'
    else:
        d['状态'] = '✅ 一致'
    # 备注 (重复警告)
    dups = dup_warnings.get(r['Container'], [])
    d['备注'] = ' | '.join(dups) if dups else ''
    # 排序权重: 差异 > 重复 > 单边 > 一致
    sort_key = 0
    if r['_mismatch'] is True: sort_key = 0
    elif dups: sort_key = 1
    elif r['_mismatch'] is None: sort_key = 2
    else: sort_key = 3
    d['_sort'] = sort_key
    df_data.append(d)

df = pd.DataFrame(df_data).sort_values('_sort').drop(columns=['_sort'])

# Highlight styling
def highlight_status(val):
    if '差异' in str(val):
        return 'background-color: #ffcccc; color: #cc0000; font-weight: bold'
    if '一致' in str(val):
        return 'background-color: #ccffcc; color: #006600'
    if '单边' in str(val):
        return 'background-color: #ffffcc; color: #996600'
    return ''

def highlight_remark(val):
    if val and '🔁' in str(val):
        return 'background-color: #ffe0cc; color: #cc6600'
    return ''

styler = df.style.map(highlight_status, subset=['状态'])
styler = styler.map(highlight_remark, subset=['备注'])
st.dataframe(styler, use_container_width=True, hide_index=True, height=min(35 * len(df) + 38, 600))

# ============================================================
# Detail for mismatches
# ============================================================
mismatch_rows = [r for r in rows if r['_mismatch'] is True]
partial_rows = [r for r in rows if r['_mismatch'] is None]

if mismatch_rows:
    st.subheader(f"⚠️ 封号不一致 ({len(mismatch_rows)} 柜)")
    for r in mismatch_rows:
        with st.expander(f"{r['Container']} — 封号不匹配"):
            cols = st.columns(len(parsed))
            for idx, p in enumerate(parsed):
                with cols[idx]:
                    st.caption(p['display_name'])
                    st.text(r.get(p['display_name'], 'N/A'))

if partial_rows:
    st.subheader(f"❓ 仅部分文件有 ({len(partial_rows)} 柜)")
    partial_text = ", ".join(r['Container'] for r in partial_rows)
    st.caption(f"仅在部分文件中出现的集装箱: {partial_text}")

if not mismatch_rows and not partial_rows:
    st.success(f"✅ 所有 {len(all_keys)} 个集装箱 — Container No 和 Seal No 完全一致！")

# 记录使用
if not st.session_state.activated:
    st.session_state.usage_today += 1

# ============================================================
# Export
# ============================================================
st.divider()
c1, c2 = st.columns(2)
with c1:
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='比对结果', index=False)
        if mismatch_rows:
            pd.DataFrame([{
                'Container': r['Container'],
                'Detail': r.get('_detail', '')
            } for r in mismatch_rows]).to_excel(writer, sheet_name='差异明细', index=False)
    st.download_button(
        "📥 导出 Excel 报告", output.getvalue(),
        file_name="单据比对报告.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
with c2:
    csv = df.to_csv(index=False)
    st.download_button(
        "📋 导出 CSV", csv,
        file_name="单据比对结果.csv",
        mime="text/csv"
    )
