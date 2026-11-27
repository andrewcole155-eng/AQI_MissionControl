import os
import json
import time
from datetime import datetime
import pytz
import numpy as np
import pandas as pd
import streamlit as st
import plotly.graph_objects as go
import gspread

# --- Canonical Search Bounds & Static Defaults (Synced with ga_risk_evolver.py) ---
GA_GENE_BOUNDS = {
    "RATCHET_TIER1_MFE_R":          (0.45,  0.90),
    "CORE_RUNNER_TRIGGER_R":        (0.95,  1.50),
    "RATCHET_TIER3_TRAIL_OFFSET_R": (0.20,  0.55),
    "STAGNATION_DAYS":              (4,     10),
    "DEGRADED_HIGH_VOL_CONF_GATE":  (0.48,  0.68),
    "OPPOSITE_SIGNAL_EXIT_CONF":    (0.45,  0.70),
    "KELLY_MULT_MAX":               (1.10,  1.60),
    "CFC_TRAIL_GAIN":               (0.00,  0.25),
    "CFC_TAU_SCALE":                (0.25,  2.50),
    "CFC_VOL_SENS":                 (-1.50, 1.50),
    "CFC_MOM_SENS":                 (-1.50, 1.50),
}

GA_STATIC_DEFAULTS = {
    "RATCHET_TIER1_MFE_R": 0.60,
    "CORE_RUNNER_TRIGGER_R": 1.10,
    "RATCHET_TIER2_MFE_R": 1.10,
    "RATCHET_TIER3_TRAIL_OFFSET_R": 0.30,
    "STAGNATION_DAYS": 7,
    "DEGRADED_HIGH_VOL_CONF_GATE": 0.55,
    "OPPOSITE_SIGNAL_EXIT_CONF": 0.55,
    "KELLY_MULT_MAX": 1.50,
    "CFC_TRAIL_GAIN": 0.00,
    "CFC_TAU_SCALE": 1.00,
    "CFC_VOL_SENS": 0.00,
    "CFC_MOM_SENS": 0.00,
}


# ==============================================================================
# 1. GOOGLE SHEETS CLOUD BRIDGE & AUDIT TRAIL I/O
# ==============================================================================
@st.cache_data(ttl=60)
def load_ga_governance_bundle(candidate_path, active_path):
    """Loads Candidate (A1), Active Promoted (B1), Decision State (C1), and Audit Log."""
    candidate_data, active_data, decision_state, audit_df = None, None, None, pd.DataFrame()
    try:
        credentials = st.secrets["gcp_service_account"]
        gc = gspread.service_account_from_dict(credentials)
        sh = gc.open("Angel_Bot_Logs")
        ws = sh.worksheet("GA_Governance")
        row_vals = ws.row_values(1)
        if len(row_vals) >= 1 and row_vals[0].strip():
            candidate_data = json.loads(row_vals[0])
        if len(row_vals) >= 2 and row_vals[1].strip():
            active_data = json.loads(row_vals[1])
        if len(row_vals) >= 3 and row_vals[2].strip():
            decision_state = json.loads(row_vals[2])

        try:
            ws_audit = sh.worksheet("GA_Audit_Log")
            recs = ws_audit.get_all_records()
            if recs:
                audit_df = pd.DataFrame(recs)
        except gspread.exceptions.WorksheetNotFound:
            pass
        return candidate_data, active_data, decision_state, audit_df
    except Exception:
        pass

    # Local disk fallback
    if os.path.exists(candidate_path):
        try:
            with open(candidate_path, "r") as f:
                candidate_data = json.load(f)
        except Exception:
            pass
    if os.path.exists(active_path):
        try:
            with open(active_path, "r") as f:
                active_data = json.load(f)
        except Exception:
            pass
    return candidate_data, active_data, decision_state, audit_df


def _append_audit_record(sh, record_dict):
    """Appends an immutable decision row to the GA_Audit_Log worksheet."""
    headers = [
        "Timestamp_UTC", "Candidate_ID", "Reviewer", "System_Verdict",
        "Human_Decision", "Override", "OOS_Fitness", "OOS_Sortino",
        "OOS_MaxDD_Pct", "Rationale"
    ]
    try:
        ws_audit = sh.worksheet("GA_Audit_Log")
    except gspread.exceptions.WorksheetNotFound:
        ws_audit = sh.add_worksheet(title="GA_Audit_Log", rows="500", cols=str(len(headers)))
        ws_audit.append_row(headers)

    row = [record_dict.get(h, "") for h in headers]
    ws_audit.append_row(row, value_input_option="RAW")


def submit_governance_decision(candidate_payload, decision, reviewer, rationale, system_verdict):
    """Executes PROMOTE, HOLD, DECLINE, or ROLLBACK and logs to GA_Audit_Log."""
    now_iso = datetime.now(pytz.utc).isoformat()
    cand_id = candidate_payload.get("candidate_id", f"GA-{candidate_payload.get('generated_at_utc', '')[:10]}") if candidate_payload else "STATIC_ROLLBACK"
    oos = candidate_payload.get("oos_comparison", {}) if candidate_payload else {}
    e_st = oos.get("evolved_stats", {})

    is_override = (
        (decision == "PROMOTED" and system_verdict != "PROMOTE")
        or (decision == "DECLINED" and system_verdict == "PROMOTE")
    )

    try:
        credentials = st.secrets["gcp_service_account"]
        gc = gspread.service_account_from_dict(credentials)
        sh = gc.open("Angel_Bot_Logs")
        try:
            ws = sh.worksheet("GA_Governance")
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title="GA_Governance", rows="5", cols="4")

        decision_meta = {
            "candidate_id": cand_id,
            "status": decision,
            "reviewer": reviewer,
            "rationale": rationale,
            "system_verdict": system_verdict,
            "is_override": is_override,
            "decided_at_utc": now_iso,
        }

        if decision == "PROMOTED":
            promoted_payload = json.loads(json.dumps(candidate_payload))
            promoted_payload["promoted_at_utc"] = now_iso
            promoted_payload["promoted_via"] = "Mission_Control_Governance"
            promoted_payload["promoted_by"] = reviewer
            promoted_payload["promotion_rationale"] = rationale
            ws.update(range_name="B1", values=[[json.dumps(promoted_payload)]])
            ws.update(range_name="C1", values=[[json.dumps(decision_meta)]])
        elif decision == "ROLLBACK":
            ws.update(range_name="B1", values=[[""]])
            ws.update(range_name="C1", values=[[json.dumps(decision_meta)]])
        else:
            # DECLINED or HELD: leave B1 untouched, record decision in C1
            ws.update(range_name="C1", values=[[json.dumps(decision_meta)]])

        _append_audit_record(sh, {
            "Timestamp_UTC": now_iso[:19],
            "Candidate_ID": cand_id,
            "Reviewer": reviewer,
            "System_Verdict": system_verdict,
            "Human_Decision": decision,
            "Override": "YES" if is_override else "NO",
            "OOS_Fitness": round(float(oos.get("evolved_fitness", 0.0)), 3),
            "OOS_Sortino": round(float(e_st.get("sortino", 0.0)), 3),
            "OOS_MaxDD_Pct": f"{float(e_st.get('max_dd', 0.0)) * 100:.2f}%",
            "Rationale": rationale or "Standard governance action",
        })
        load_ga_governance_bundle.clear()
        return True, f"Governance decision '{decision}' recorded for {cand_id} and logged to GA_Audit_Log."
    except Exception as e:
        return False, f"Governance write failed: {e}"


# ==============================================================================
# 2. DETERMINISTIC CRITERIA MATRIX & EXPLAINABILITY ENGINE (ZERO LLM)
# ==============================================================================
def evaluate_criteria_and_explainability(ga_candidate, ga_active):
    """Deterministically builds the Criteria Evidence Matrix and executive explanation."""
    oos = ga_candidate.get("oos_comparison", {})
    train = ga_candidate.get("train_comparison", {})
    b_st = oos.get("baseline_stats", {})
    e_st = oos.get("evolved_stats", {})
    inc_st = oos.get("incumbent_stats", b_st)
    has_incumbent = bool(ga_candidate.get("has_active_incumbent", False) or ga_active)

    b_fit = float(oos.get("baseline_fitness", 0.0))
    e_fit = float(oos.get("evolved_fitness", 0.0))
    if ga_candidate.get("has_active_incumbent", False):
        inc_fit = float(oos.get("incumbent_fitness", b_fit))
    elif ga_active and "oos_comparison" in ga_active:
        inc_fit = float(ga_active["oos_comparison"].get("evolved_fitness", b_fit))
    else:
        inc_fit = b_fit
    b_roll = float(oos.get("baseline_rolling_fitness", 0.0))
    e_roll = float(oos.get("evolved_rolling_fitness", 0.0))
    is_fit = float(train.get("evolved_fitness", e_fit))

    b_dd = float(b_st.get("max_dd", 0.0))
    e_dd = float(e_st.get("max_dd", 0.0))
    e_trades = int(e_st.get("trades", 0))
    top2_share = float(e_st.get("top_2_profit_share", 0.0))
    pinned = ga_candidate.get("boundary_pinned_genes", [])
    e_folds = ga_candidate.get("rolling_folds", {}).get("evolved", [])
    neg_folds = sum(1 for f in e_folds if float(f.get("fitness", 0.0)) < 0)

    # --- Hard Gates (Preserving Exact Existing Thresholds) ---
    g1_margin = e_fit - (b_fit + 0.10)
    g1_pass = e_fit > (b_fit + 0.10)

    g2_margin = (e_dd - (b_dd - 0.02)) * 100.0
    g2_pass = e_dd >= (b_dd - 0.02)

    g3_margin = e_roll - b_roll
    g3_pass = e_roll >= b_roll

    # --- Advisory Checks (Trigger HOLD FOR REVIEW if violated) ---
    adv_inc_pass = (not has_incumbent) or (e_fit >= inc_fit - 0.05)
    adv_sample_pass = e_trades >= 10
    adv_conc_pass = (top2_share == 0.0) or (top2_share <= 0.65)
    adv_pin_pass = len(pinned) < 2
    overfit_gap = is_fit - e_fit if train else 0.0
    adv_overfit_pass = (not train) or (overfit_gap <= 2.5)

    criteria = [
        {
            "Type": "HARD GATE",
            "Requirement": "OOS Fitness Improvement",
            "Candidate": f"{e_fit:.3f}",
            "Threshold": f"> {b_fit + 0.10:.3f} (Base + 0.10)",
            "Margin": f"{g1_margin:+.3f}",
            "Status": "✅ PASS" if g1_pass else "❌ FAIL",
            "Consequence": "Blocks promotion if failed",
        },
        {
            "Type": "HARD GATE",
            "Requirement": "OOS Max Drawdown Preservation",
            "Candidate": f"{e_dd:.2%}",
            "Threshold": f">= {b_dd - 0.02:.2%} (Base - 2.0%)",
            "Margin": f"{g2_margin:+.2f}%",
            "Status": "✅ PASS" if g2_pass else "❌ FAIL",
            "Consequence": "Blocks promotion if failed",
        },
        {
            "Type": "HARD GATE",
            "Requirement": "3-Fold Rolling Mean Fitness",
            "Candidate": f"{e_roll:.3f}",
            "Threshold": f">= {b_roll:.3f} (Baseline)",
            "Margin": f"{g3_margin:+.3f}",
            "Status": "✅ PASS" if g3_pass else "❌ FAIL",
            "Consequence": "Blocks promotion if failed",
        },
        {
            "Type": "ADVISORY",
            "Requirement": "Beats Active Incumbent (OOS)",
            "Candidate": f"{e_fit:.3f}",
            "Threshold": f">= {inc_fit:.3f} (Incumbent)" if has_incumbent else "No active incumbent",
            "Margin": f"{e_fit - inc_fit:+.3f}" if has_incumbent else "N/A",
            "Status": "✅ PASS" if adv_inc_pass else "⚠️ REVIEW",
            "Consequence": "Flags Hold if inferior to live GA",
        },
        {
            "Type": "ADVISORY",
            "Requirement": "OOS Statistical Sample Size",
            "Candidate": f"{e_trades} trades",
            "Threshold": ">= 10 OOS trades",
            "Margin": f"{e_trades - 10:+d} trades",
            "Status": "✅ PASS" if adv_sample_pass else "⚠️ REVIEW",
            "Consequence": "Lowers evidence confidence",
        },
        {
            "Type": "ADVISORY",
            "Requirement": "IS-to-OOS Generalisation Gap",
            "Candidate": f"{overfit_gap:+.2f}" if train else "Legacy JSON (N/A)",
            "Threshold": "<= +2.50 fitness drop",
            "Margin": f"{2.50 - overfit_gap:+.2f}" if train else "N/A",
            "Status": "✅ PASS" if adv_overfit_pass else "⚠️ REVIEW",
            "Consequence": "Signals potential overfitting",
        },
        {
            "Type": "ADVISORY",
            "Requirement": "Top-2 Trade Profit Concentration",
            "Candidate": f"{top2_share:.0%}" if top2_share > 0 else "Legacy JSON (N/A)",
            "Threshold": "<= 65% of gross profit",
            "Margin": f"{(0.65 - top2_share)*100:+.1f}%" if top2_share > 0 else "N/A",
            "Status": "✅ PASS" if adv_conc_pass else "⚠️ REVIEW",
            "Consequence": "Highlights outlier dependency",
        },
        {
            "Type": "ADVISORY",
            "Requirement": "Parameter Boundary Pinning",
            "Candidate": f"{len(pinned)} pinned",
            "Threshold": "< 2 genes at hard walls",
            "Margin": f"{1 - len(pinned):+d}",
            "Status": "✅ PASS" if adv_pin_pass else "⚠️ REVIEW",
            "Consequence": "Suggests search-bound stress",
        },
    ]

    hard_passed = sum(1 for c in criteria if c["Type"] == "HARD GATE" and "PASS" in c["Status"])
    hard_failed = sum(1 for c in criteria if c["Type"] == "HARD GATE" and "FAIL" in c["Status"])
    adv_reviews = sum(1 for c in criteria if "REVIEW" in c["Status"])

    if hard_failed > 0:
        verdict = "DECLINE"
    elif adv_reviews > 0:
        verdict = "HOLD FOR REVIEW"
    else:
        verdict = "PROMOTE"

    # Confidence Rating
    if e_trades >= 20 and ga_candidate.get("timescaledb_signals_loaded", 0) > 0:
        confidence_label = "🟢 HIGH"
    elif e_trades >= 10:
        confidence_label = "🟡 MODERATE"
    else:
        confidence_label = "🔴 LOW (Small Sample)"

    # Deterministic Executive Explanation
    strengths, risks = [], []
    if e_fit > b_fit:
        strengths.append(f"OOS fitness improved from {b_fit:.3f} to {e_fit:.3f} (Sortino {float(b_st.get('sortino', 0)):.2f} → {float(e_st.get('sortino', 0)):.2f})")
    if e_dd > b_dd:
        strengths.append(f"OOS max drawdown narrowed from {b_dd:.2%} to {e_dd:.2%}")
    if e_roll > b_roll:
        strengths.append(f"3-fold rolling mean fitness rose from {b_roll:.3f} to {e_roll:.3f}")

    if not g1_pass:
        risks.append(f"failed the +0.10 OOS fitness hurdle (actual lift {e_fit - b_fit:+.3f})")
    if not g2_pass:
        risks.append(f"max drawdown deteriorated beyond the 2.0% tolerance ({e_dd:.2%} vs {b_dd:.2%})")
    if not g3_pass:
        risks.append(f"3-fold rolling fitness ({e_roll:.3f}) underperformed baseline ({b_roll:.3f})")
    if not adv_sample_pass:
        risks.append(f"OOS trade count is thin ({e_trades} trades), limiting statistical certainty")
    if not adv_conc_pass:
        risks.append(f"top 2 winning trades account for {top2_share:.0%} of OOS gross profit")
    if not adv_inc_pass:
        risks.append(f"candidate OOS fitness ({e_fit:.3f}) trails the currently active incumbent ({inc_fit:.3f})")
    if not adv_overfit_pass:
        risks.append(f"large IS-to-OOS fitness drop ({is_fit:.2f} → {e_fit:.2f}) indicates possible overfitting")
    if neg_folds >= 2:
        risks.append(f"{neg_folds} of {len(e_folds)} rolling folds were negative")

    why_promote = "; ".join(strengths) if strengths else "No material out-of-sample improvement over static defaults."
    what_wrong = "; ".join(risks) if risks else "No critical fragility flags detected within current OOS sample; monitor live trade frequency after activation."

    return {
        "verdict": verdict,
        "criteria_df": pd.DataFrame(criteria),
        "hard_passed": hard_passed,
        "hard_failed": hard_failed,
        "adv_reviews": adv_reviews,
        "confidence": confidence_label,
        "why_promote": why_promote,
        "what_wrong": what_wrong,
    }


# ==============================================================================
# 3. PLOTLY VISUALISATION BUILDERS
# ==============================================================================
def build_gene_bounds_dumbbell(cand_genes, active_genes):
    """Plots each gene's position within its search bounds (Default vs Active vs Candidate)."""
    fig = go.Figure()
    gene_names = list(GA_GENE_BOUNDS.keys())

    for g in gene_names:
        lo, hi = GA_GENE_BOUNDS[g]
        span = max(hi - lo, 1e-6)
        norm = lambda v: ((float(v) - lo) / span) * 100.0

        b_v = GA_STATIC_DEFAULTS[g]
        a_v = active_genes.get(g, b_v) if active_genes else b_v
        c_v = cand_genes.get(g, a_v) if cand_genes else a_v

        # Track bar (0% to 100% of search space)
        fig.add_trace(go.Scatter(
            x=[0, 100], y=[g, g], mode="lines",
            line=dict(color="#3a3a3a", width=6), showlegend=False, hoverinfo="skip"
        ))
        # Shift connector from Active to Candidate
        fig.add_trace(go.Scatter(
            x=[norm(a_v), norm(c_v)], y=[g, g], mode="lines",
            line=dict(color="#00ff41", width=3), showlegend=False, hoverinfo="skip"
        ))

    def _add_pts(label, vals_dict, color, symbol, size):
        xs, texts = [], []
        for g in gene_names:
            lo, hi = GA_GENE_BOUNDS[g]
            v = vals_dict.get(g, GA_STATIC_DEFAULTS[g])
            xs.append(((float(v) - lo) / max(hi - lo, 1e-6)) * 100.0)
            texts.append(f"{g}<br>{label}: {v} (Range: {lo}–{hi})")
        fig.add_trace(go.Scatter(
            x=xs, y=gene_names, mode="markers", name=label,
            marker=dict(color=color, size=size, symbol=symbol, line=dict(color="#111", width=1)),
            hovertext=texts, hoverinfo="text"
        ))

    _add_pts("Static Default", GA_STATIC_DEFAULTS, "#888888", "circle", 9)
    if active_genes:
        _add_pts("Active Incumbent", active_genes, "#569cd6", "square", 10)
    if cand_genes:
        _add_pts("Staged Candidate", cand_genes, "#00ff41", "diamond", 13)

    fig.add_vline(x=2, line_dash="dot", line_color="#ffb000", opacity=0.4)
    fig.add_vline(x=98, line_dash="dot", line_color="#ffb000", opacity=0.4)
    fig.update_layout(
        title=dict(text="Gene Position Within Search Bounds (0% = Lower Wall, 100% = Upper Wall)", y=0.96),
        xaxis=dict(title="Normalized Position in Search Space (%)", range=[-5, 105], gridcolor="#333"),
        yaxis=dict(autorange="reversed", gridcolor="#2a2a2a"),
        height=390, margin=dict(l=10, r=10, t=65, b=30),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#cccccc"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0)
    )
    return fig


def build_oos_equity_chart(oos_curves, has_incumbent):
    fig = go.Figure()
    base_eq = oos_curves.get("baseline_equity", [])
    inc_eq = oos_curves.get("incumbent_equity", [])
    evo_eq = oos_curves.get("evolved_equity", [])

    if base_eq:
        fig.add_trace(go.Scatter(y=[(x - 1) * 100 for x in base_eq], mode="lines+markers", name="Static Baseline", line=dict(color="#888888", dash="dot", width=2)))
    if has_incumbent and inc_eq:
        fig.add_trace(go.Scatter(y=[(x - 1) * 100 for x in inc_eq], mode="lines+markers", name="Active Incumbent", line=dict(color="#569cd6", width=2.5)))
    if evo_eq:
        fig.add_trace(go.Scatter(y=[(x - 1) * 100 for x in evo_eq], mode="lines+markers", name="Staged Candidate", line=dict(color="#00ff41", width=3)))

    fig.add_hline(y=0, line_dash="dash", line_color="rgba(255,255,255,0.3)")
    fig.update_layout(
        title=dict(text="Out-of-Sample Cumulative Equity Replay (%)", y=0.96),
        xaxis=dict(title="Executed OOS Trade Sequence", gridcolor="#333"),
        yaxis=dict(title="Cumulative Return (%)", gridcolor="#333"),
        height=320, margin=dict(l=10, r=10, t=65, b=30),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#cccccc"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0)
    )
    return fig


def build_rolling_folds_chart(rolling_folds, has_incumbent):
    fig = go.Figure()
    b_f = rolling_folds.get("baseline", [])
    i_f = rolling_folds.get("incumbent", [])
    e_f = rolling_folds.get("evolved", [])
    labels = [f["fold"] for f in e_f]

    if b_f:
        fig.add_trace(go.Bar(x=labels, y=[f["fitness"] for f in b_f], name="Static Baseline", marker_color="#666666"))
    if has_incumbent and i_f:
        fig.add_trace(go.Bar(x=labels, y=[f["fitness"] for f in i_f], name="Active Incumbent", marker_color="#569cd6"))
    if e_f:
        fig.add_trace(go.Bar(x=labels, y=[f["fitness"] for f in e_f], name="Staged Candidate", marker_color="#00ff41"))

    fig.add_hline(y=0, line_dash="dash", line_color="white")
    fig.update_layout(
        title=dict(text="3-Fold Chronological Walk-Forward Fitness Dispersion", y=0.96),
        barmode="group", height=310, margin=dict(l=10, r=10, t=65, b=30),
        yaxis=dict(title="Fold Fitness", gridcolor="#333"),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#cccccc"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0)
    )
    return fig


def build_sensitivity_chart(sensitivity_dict):
    genes = list(sensitivity_dict.keys())
    drops = [float(sensitivity_dict[g].get("max_drop", 0.0)) for g in genes]
    colors_bar = ["#ff4b4b" if d > 0.75 else ("#ffb000" if d > 0.30 else "#00ff41") for d in drops]

    fig = go.Figure(go.Bar(
        y=genes, x=drops, orientation="h", marker_color=colors_bar,
        text=[f"-{d:.2f}" for d in drops], textposition="outside"
    ))
    fig.update_layout(
        title="Local Parameter Fragility (Max OOS Fitness Drop at ±10% Perturbation)",
        xaxis=dict(title="Worst-Case Fitness Drop (Lower = More Robust)", gridcolor="#333"),
        yaxis=dict(autorange="reversed"),
        height=360, margin=dict(l=10, r=30, t=40, b=30),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#cccccc")
    )
    return fig


def build_return_distribution_chart(oos_curves):
    fig = go.Figure()
    b_r = [x * 100 for x in oos_curves.get("baseline_trade_returns", [])]
    e_r = [x * 100 for x in oos_curves.get("evolved_trade_returns", [])]

    if b_r:
        fig.add_trace(go.Box(x=b_r, name="Static Baseline", boxpoints="all", jitter=0.3, marker_color="#888888"))
    if e_r:
        fig.add_trace(go.Box(x=e_r, name="Staged Candidate", boxpoints="all", jitter=0.3, marker_color="#00ff41"))

    fig.add_vline(x=0, line_dash="dash", line_color="white")
    fig.update_layout(
        title="OOS Per-Trade Return Distribution (% Equity Impact per Trade)",
        xaxis=dict(title="Single-Trade Portfolio Return (%)", gridcolor="#333"),
        height=270, margin=dict(l=10, r=10, t=40, b=30),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#cccccc"), showlegend=False
    )
    return fig


# ==============================================================================
# 4. MAIN RENDER ENTRYPOINT FOR MISSION CONTROL (TAB 6)
# ==============================================================================
def render_ga_governance_panel(candidate_path, active_path):
    st.markdown("### 🧬 Institutional GA Risk Gate Governance")
    ga_candidate, ga_active, decision_state, audit_df = load_ga_governance_bundle(candidate_path, active_path)

    if not ga_candidate and not ga_active:
        st.info("No GA candidate or active risk gate payload found in `GA_Governance!A1/B1`. Run `optimization/ga_risk_evolver.py` to generate one.")
        st.divider()
        return

    active_genes = ga_active.get("candidate_genes", {}) if ga_active else {}
    cand_genes = ga_candidate.get("candidate_genes", {}) if ga_candidate else {}
    is_synced = bool(ga_candidate and ga_active and active_genes == cand_genes)
    cand_id = ga_candidate.get("candidate_id", f"GA-{ga_candidate.get('generated_at_utc', 'N/A')[:10]}") if ga_candidate else "ACTIVE-ONLY"

    eval_res = evaluate_criteria_and_explainability(ga_candidate or ga_active, ga_active)
    verdict = eval_res["verdict"]
    verdict_color = "#00ff41" if verdict == "PROMOTE" else ("#ff4b4b" if verdict == "DECLINE" else "#ffb000")

    # --------------------------------------------------------------------------
    # A. CANDIDATE EXECUTIVE DECISION PANEL & DETERMINISTIC EXPLAINABILITY
    # --------------------------------------------------------------------------
    ex_left, ex_right = st.columns([1.3, 2.7])
    with ex_left:
        sync_badge = "🟢 LIVE & SYNCED" if is_synced else ("🟡 STAGED (UNPROMOTED)" if ga_candidate else "🔵 ACTIVE ONLY")
        prior_dec = decision_state.get("status") if (decision_state and decision_state.get("candidate_id") == cand_id) else "PENDING REVIEW"

        st.markdown(f"""
        <div style="background:#181818; border:1px solid #333; border-left:5px solid {verdict_color}; border-radius:8px; padding:14px;">
            <div style="font-size:11px; color:#888; font-family:monospace;">CANDIDATE ID: {cand_id}</div>
            <div style="font-size:22px; font-weight:bold; color:{verdict_color}; margin:4px 0;">{verdict}</div>
            <div style="font-size:12px; color:#ccc; margin-bottom:8px;">
                <strong>Production Sync:</strong> {sync_badge}<br>
                <strong>Human Status:</strong> {prior_dec}<br>
                <strong>Evidence Confidence:</strong> {eval_res['confidence']}<br>
                <strong>Gates:</strong> {eval_res['hard_passed']}/3 Hard Passed &nbsp;|&nbsp; {eval_res['adv_reviews']} Advisory
            </div>
        </div>
        """, unsafe_allow_html=True)

    with ex_right:
        st.markdown(f"""
        <div style="background:#1e1e1e; border:1px solid #333; border-radius:8px; padding:14px; height:100%;">
            <div style="font-size:13px; font-weight:bold; color:#569cd6; margin-bottom:6px;">
                🧠 DETERMINISTIC DECISION EXPLAINABILITY (ZERO-LLM RULE ENGINE)
            </div>
            <div style="font-size:12.5px; color:#ddd; line-height:1.55;">
                <strong>1. Why promote:</strong> {eval_res['why_promote']}<br>
                <strong>2. What could go wrong:</strong> {eval_res['what_wrong']}<br>
                <strong>3. Evidence basis:</strong> Evaluated across <strong>{(ga_candidate or ga_active).get('episodes_evaluated', 0)}</strong> chronological Neo4j episodes
                (<strong>{(ga_candidate or ga_active).get('timescaledb_signals_loaded', 0)}</strong> TimescaleDB signals loaded).
            </div>
        </div>
        """, unsafe_allow_html=True)

    # --------------------------------------------------------------------------
    # B. GOVERNANCE CRITERIA & EVIDENCE MATRIX
    # --------------------------------------------------------------------------
    with st.expander("📋 Governance Criteria & Evidence Matrix (Hard Gates & Advisory Checks)", expanded=(not is_synced)):
        st.dataframe(eval_res["criteria_df"], width="stretch", hide_index=True)

    # --------------------------------------------------------------------------
    # C. THREE-TAB ANALYTICAL DRILL-DOWN
    # --------------------------------------------------------------------------
    g_tab1, g_tab2, g_tab3 = st.tabs([
        "📊 Candidate vs. Incumbent & Baseline",
        "🧪 Robustness & Generalisation",
        "⚠️ Risk, Failure & Return Distribution"
    ])

    oos = (ga_candidate or ga_active).get("oos_comparison", {})
    b_st = oos.get("baseline_stats", {})
    e_st = oos.get("evolved_stats", {})
    inc_st = oos.get("incumbent_stats", b_st)
    has_inc = bool((ga_candidate or {}).get("has_active_incumbent", False))
    oos_curves = (ga_candidate or {}).get("oos_curves", {})

    with g_tab1:
        # Core OOS KPI Strip
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("OOS Fitness", f"{oos.get('evolved_fitness', 0.0):.3f}", f"{oos.get('evolved_fitness', 0.0) - oos.get('baseline_fitness', 0.0):+.3f} vs Base")
        m2.metric("OOS Sortino", f"{e_st.get('sortino', 0.0):.3f}", f"{e_st.get('sortino', 0.0) - b_st.get('sortino', 0.0):+.3f} vs Base")
        m3.metric("OOS Max Drawdown", f"{e_st.get('max_dd', 0.0):.2%}", f"{(e_st.get('max_dd', 0.0) - b_st.get('max_dd', 0.0))*100:+.2f}% vs Base")
        m4.metric("Profit Factor", f"{e_st.get('profit_factor', 0.0):.2f}", f"Win Rate: {e_st.get('win_rate', 0.0):.0%}", delta_color="off")
        m5.metric("3-Fold Rolling Fit", f"{oos.get('evolved_rolling_fitness', 0.0):.3f}", f"{oos.get('evolved_rolling_fitness', 0.0) - oos.get('baseline_rolling_fitness', 0.0):+.3f} vs Base")

        c_cmp1, c_cmp2 = st.columns([1.2, 1.3])
        with c_cmp1:
            if oos_curves.get("evolved_equity"):
                st.plotly_chart(build_oos_equity_chart(oos_curves, has_inc), width="stretch")
            else:
                st.info("Re-run `ga_risk_evolver.py` (V3) to populate OOS equity curve overlays.")
        with c_cmp2:
            st.plotly_chart(build_gene_bounds_dumbbell(cand_genes, active_genes), width="stretch")

        # Parameter Table
        gene_rows = []
        for k in GA_STATIC_DEFAULTS.keys():
            s_v = GA_STATIC_DEFAULTS[k]
            a_v = active_genes.get(k, s_v) if ga_active else s_v
            c_v = cand_genes.get(k, None)
            gene_rows.append({
                "Risk Parameter": k,
                "Static Default": s_v,
                "Active Live": a_v,
                "Staged Candidate": c_v if c_v is not None else "—",
                "Delta (Cand vs Live)": f"{c_v - a_v:+.3f}" if c_v is not None else "—",
                "Delta (Cand vs Static)": f"{c_v - s_v:+.3f}" if c_v is not None else "—",
            })
        st.dataframe(pd.DataFrame(gene_rows), width="stretch", hide_index=True)

    with g_tab2:
        train = (ga_candidate or {}).get("train_comparison", {})
        r_folds = (ga_candidate or {}).get("rolling_folds", {})
        sens = (ga_candidate or {}).get("parameter_sensitivity", {})

        rc1, rc2, rc3 = st.columns(3)
        if train:
            is_f = float(train.get("evolved_fitness", 0.0))
            oo_f = float(oos.get("evolved_fitness", 0.0))
            rc1.metric("In-Sample (Train) Fitness", f"{is_f:.3f}", f"Base IS: {train.get('baseline_fitness', 0.0):.3f}", delta_color="off")
            rc2.metric("Out-of-Sample Fitness", f"{oo_f:.3f}", f"IS→OOS Gap: {oo_f - is_f:+.3f}", delta_color="normal" if (is_f - oo_f) <= 2.5 else "inverse")
            rc3.metric("3-Fold Fitness Dispersion (Std)", f"{oos.get('evolved_rolling_std', 0.0):.3f}", f"Base Std: {oos.get('baseline_rolling_std', 0.0):.3f}", delta_color="off")

        g_rob1, g_rob2 = st.columns(2)
        with g_rob1:
            if r_folds.get("evolved"):
                st.plotly_chart(build_rolling_folds_chart(r_folds, has_inc), width="stretch")
            else:
                st.info("Re-run `ga_risk_evolver.py` (V3) to populate per-fold walk-forward dispersion.")
        with g_rob2:
            if sens:
                st.plotly_chart(build_sensitivity_chart(sens), width="stretch")
            else:
                st.info("Re-run `ga_risk_evolver.py` (V3) to populate ±10% parameter sensitivity diagnostics.")

    with g_tab3:
        rk1, rk2, rk3, rk4, rk5 = st.columns(5)
        rk1.metric("Max Consec. Losses", f"{e_st.get('max_consecutive_losses', 0)}", f"Base: {b_st.get('max_consecutive_losses', 0)}", delta_color="off")
        rk2.metric("Worst Single Trade", f"{e_st.get('worst_trade_ret', 0.0):.2%}", f"Base: {b_st.get('worst_trade_ret', 0.0):.2%}", delta_color="off")
        rk3.metric("Trade CVaR (95%)", f"{e_st.get('cvar_95_trade', 0.0):.2%}", f"Base: {b_st.get('cvar_95_trade', 0.0):.2%}", delta_color="off")
        rk4.metric("Top-2 Profit Share", f"{e_st.get('top_2_profit_share', 0.0):.0%}", "Fragile if > 65%", delta_color="inverse" if e_st.get("top_2_profit_share", 0.0) > 0.65 else "off")
        rk5.metric("Max DD Duration", f"{e_st.get('max_dd_duration_trades', 0)} tr", f"Giveback: {e_st.get('avg_giveback_r', 0.0):.2f}R", delta_color="off")

        if oos_curves.get("evolved_trade_returns"):
            st.plotly_chart(build_return_distribution_chart(oos_curves), width="stretch")
        else:
            st.info("Re-run `ga_risk_evolver.py` (V3) to populate OOS trade return distribution plots.")

    # --------------------------------------------------------------------------
    # D. HUMAN-IN-THE-LOOP GOVERNANCE CONSOLE & AUDIT TRAIL
    # --------------------------------------------------------------------------
    st.markdown("#### ⚖️ Human-in-the-Loop Governance Action")
    act_c1, act_c2, act_c3 = st.columns([1.2, 1.3, 2.5])
    with act_c1:
        reviewer_name = st.text_input("Reviewer", value="Andrew Cole", key="ga_reviewer_input")
    with act_c2:
        action_choice = st.radio(
            "Governance Decision",
            ["PROMOTE", "HOLD FOR REVIEW", "DECLINE"],
            index=0 if verdict == "PROMOTE" else (1 if verdict == "HOLD FOR REVIEW" else 2),
            horizontal=True,
            key="ga_decision_radio"
        )
    with act_c3:
        is_override_choice = (
            (action_choice == "PROMOTE" and verdict != "PROMOTE")
            or (action_choice == "DECLINE" and verdict == "PROMOTE")
        )
        rationale_label = "Decision Rationale (REQUIRED — Overriding System Verdict)" if is_override_choice else "Decision Rationale (Optional Audit Note)"
        rationale_txt = st.text_input(rationale_label, placeholder="Document key evidence or risk justification...", key="ga_rationale_input")

    confirm_promo = True
    if action_choice == "PROMOTE":
        confirm_promo = st.checkbox(
            "I confirm I have reviewed OOS robustness, drawdown, and risk concentration, and authorize live hot-reload via `GA_Governance!B1`.",
            value=False,
            key="ga_confirm_promo_chk"
        )

    b_col1, b_col2, b_col3 = st.columns([1.5, 1.5, 3])
    with b_col1:
        submit_disabled = (not ga_candidate) or (action_choice == "PROMOTE" and (is_synced or not confirm_promo))
        if st.button(f"✅ Submit: {action_choice}", type="primary", disabled=submit_disabled, key="ga_submit_decision_btn", width="stretch"):
            if is_override_choice and len(rationale_txt.strip()) < 8:
                st.error("Mandatory rationale (min 8 characters) is required when overriding the governance recommendation.")
            else:
                mapped = "PROMOTED" if action_choice == "PROMOTE" else ("HELD" if action_choice == "HOLD FOR REVIEW" else "DECLINED")
                ok, msg = submit_governance_decision(ga_candidate, mapped, reviewer_name.strip() or "Reviewer", rationale_txt.strip(), verdict)
                if ok:
                    st.success(msg)
                    time.sleep(0.8)
                    st.rerun()
                else:
                    st.error(msg)

    with b_col2:
        if st.button("⏪ Rollback to Static Defaults", disabled=(not ga_active), key="ga_rollback_btn", width="stretch"):
            ok, msg = submit_governance_decision(ga_active, "ROLLBACK", reviewer_name.strip() or "Reviewer", rationale_txt.strip() or "Manual rollback to static defaults", verdict)
            if ok:
                st.warning(msg)
                time.sleep(0.8)
                st.rerun()
            else:
                st.error(msg)

    with b_col3:
        st.caption(
            "Promotions write to `Angel_Bot_Logs → GA_Governance!B1` and hot-reload into "
            "`ALPACA_Trading_Bot_REAL_V11.py` within 5 minutes. All decisions log to `GA_Audit_Log`."
        )

    with st.expander("📜 Historical Governance Audit Trail (`GA_Audit_Log`)", expanded=False):
        if not audit_df.empty:
            st.dataframe(audit_df.iloc[::-1], width="stretch", hide_index=True)
        else:
            st.caption("No governance decisions logged yet. Your next Promote, Hold, Decline, or Rollback will initialize `GA_Audit_Log`.")

    st.divider()