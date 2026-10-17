#include "QaPanel.h"

#include "Buttons.h"
#include "Icons.h"
#include "Indicators.h"

#include <QGridLayout>
#include <QHBoxLayout>
#include <QJsonValue>
#include <QLabel>
#include <QListWidget>
#include <QPlainTextEdit>
#include <QTextCharFormat>
#include <QTextEdit>
#include <QVBoxLayout>

namespace {
const char *kKeys[] = {"not_checked", "checking", "verified", "needs_review", "failed"};
const char *kText[] = {"Not checked", "Checking…", "Verified", "Needs review", "Failed"};

AnimatedButton *btn(const QString &text, const char *icon, const char *name, const QString &tip, const char *variant = nullptr) {
    auto *b = new AnimatedButton(text);
    b->setObjectName(name);
    if (icon) b->setIcon(Icons::icon(icon));
    b->setToolTip(tip);
    if (variant) b->setProperty("variant", variant);
    return b;
}

QPlainTextEdit *textBox(const char *name, int lines) {
    auto *e = new QPlainTextEdit;
    e->setObjectName(name);
    e->setReadOnly(true);
    e->setFixedHeight(e->fontMetrics().lineSpacing() * lines + 16);
    return e;
}

QString pct(const QJsonValue &v) { return v.isNull() || v.isUndefined() ? QStringLiteral("n/a") : QString::number(v.toDouble(), 'f', 2); }
}  // namespace

QString QaPanel::statusKey(Status s) { return kKeys[int(s)]; }

QaPanel::Status QaPanel::statusFromKey(const QString &k) {
    for (int i = 0; i < 5; ++i)
        if (k == kKeys[i]) return Status(i);
    return Status::NotChecked;
}

QaPanel::QaPanel(QWidget *parent) : QWidget(parent) {
    setObjectName("qaPanel");
    auto *v = new QVBoxLayout(this);
    v->setContentsMargins(0, 0, 0, 0);
    v->setSpacing(8);

    auto *head = new QHBoxLayout;
    head->setSpacing(8);
    m_recheck = btn("Recheck Speech", "recheck", "recheckButton",
                    "Transcribe the generated audio with a speech recogniser and compare it with your text");
    m_spin = new StateIcon(18);
    m_spin->setKind(StateIcon::Kind::Busy);
    m_spin->hide();
    m_badge = new QLabel;
    m_badge->setObjectName("qaBadge");
    m_badge->setAccessibleName("Verification status");
    m_badge->setSizePolicy(QSizePolicy::Maximum, QSizePolicy::Fixed);
    head->setAlignment(Qt::AlignVCenter);
    head->addWidget(m_recheck);
    head->addWidget(m_spin);
    head->addStretch(1);
    head->addWidget(m_badge);
    v->addLayout(head);

    m_info = new QLabel;
    m_info->setObjectName("qaNote");
    m_info->setWordWrap(true);
    m_info->setSizePolicy(QSizePolicy::Preferred, QSizePolicy::Minimum);
    v->addWidget(m_info);

    m_detail = new QWidget;                          // problems, side-by-side texts, repair controls: shown once there is something to review
    auto *dv = new QVBoxLayout(m_detail);
    dv->setContentsMargins(0, 0, 0, 0);
    dv->setSpacing(8);
    m_list = new QListWidget;
    m_list->setObjectName("issueList");
    m_list->setMinimumHeight(64);
    m_list->setMaximumHeight(130);
    m_list->setAccessibleName("Suspected problems");
    dv->addWidget(m_list);

    auto *il = new QLabel("Intended text");
    il->setObjectName("sectionTitle");
    auto *rl = new QLabel("Recognized speech");
    rl->setObjectName("sectionTitle");
    m_intended = textBox("intendedText", 2);
    m_recognized = textBox("recognizedText", 2);
    m_reason = new QLabel;
    m_reason->setObjectName("qaNote");
    m_reason->setWordWrap(true);
    dv->addWidget(il);
    dv->addWidget(m_intended);
    dv->addWidget(rl);
    dv->addWidget(m_recognized);
    dv->addWidget(m_reason);

    auto *g = new QGridLayout;
    g->setSpacing(6);
    m_replay = btn("Replay", "play", "replayButton", "Play the affected segment");
    m_correct = btn("Correct…", "correct", "correctButton", "Give this word a pronunciation (respelling or IPA) and regenerate it");
    m_repairSel = btn("Repair", "repair", "repairSelectedButton", "Regenerate only the chunk with the selected problem");
    m_repairAuto = btn("Auto-repair", "ai", "autoRepairButton",
                       "Try approved corrections (dictionary, and AI suggestions if enabled) on every flagged chunk, then recheck");
    m_accept = btn("Accept repair", "accept", "acceptRepairButton", "Use the repaired audio (the original is kept in the session)", "success");
    m_discard = btn("Keep original", "cancel", "discardRepairButton", "Discard the repaired version");
    g->setColumnStretch(0, 1);
    g->setColumnStretch(1, 1);
    g->addWidget(m_replay, 0, 0);
    g->addWidget(m_correct, 0, 1);
    g->addWidget(m_repairSel, 1, 0);
    g->addWidget(m_repairAuto, 1, 1);
    g->addWidget(m_accept, 2, 0);
    g->addWidget(m_discard, 2, 1);
    dv->addLayout(g);
    v->addWidget(m_detail);
    m_detail->hide();

    v->addWidget(m_saveFb = btn("Save correction for learning", "save-feedback", "saveFeedbackButton",
                   "Store this verified correction locally (asks for consent first). Nothing is uploaded."));
    auto *foot = new QHBoxLayout;
    foot->setSpacing(6);
    m_dict = btn("Dictionary…", "dictionary", "dictionaryButton", "Edit the pronunciation dictionary");
    m_reportToggle = btn("Report", "info", "reportToggle", "Show the quality report");
    m_reportToggle->setCheckable(true);
    foot->addWidget(m_dict);
    foot->addWidget(m_reportToggle);
    foot->addStretch(1);
    v->addLayout(foot);
    m_report = new QPlainTextEdit;
    m_report->setObjectName("qaReport");
    m_report->setReadOnly(true);
    m_report->setMinimumHeight(120);
    m_report->hide();
    v->addWidget(m_report);
    m_note = new QLabel("Verification compares a speech-recognition transcript and simple audio checks with your text. "
                        "It is not proof of correct pronunciation.");
    m_note->setObjectName("qaNote");
    m_note->setWordWrap(true);
    v->addWidget(m_note);
    v->addStretch(1);                                // spare height goes below the content, never into the badge or buttons

    connect(m_recheck, &QPushButton::clicked, this, &QaPanel::recheckRequested);
    connect(m_replay, &QPushButton::clicked, this, [this] {
        const auto i = currentIssue();
        if (!i.isEmpty()) emit replayRequested(i.value("chunk").toInt());
    });
    connect(m_correct, &QPushButton::clicked, this, [this] {
        const auto i = currentIssue();
        if (!i.isEmpty()) emit correctRequested(i);
    });
    connect(m_repairSel, &QPushButton::clicked, this, [this] {
        const auto i = currentIssue();
        if (!i.isEmpty()) emit repairRequested("review", QJsonArray{i.value("chunk").toInt()});
    });
    connect(m_repairAuto, &QPushButton::clicked, this, [this] { emit repairRequested("auto", QJsonArray()); });
    connect(m_accept, &QPushButton::clicked, this, &QaPanel::acceptRepairRequested);
    connect(m_discard, &QPushButton::clicked, this, &QaPanel::discardRepairRequested);
    connect(m_dict, &QPushButton::clicked, this, &QaPanel::dictionaryRequested);
    connect(m_saveFb, &QPushButton::clicked, this, [this] {
        for (const auto &o : m_repair.value("outcomes").toArray())
            for (const auto &a : o.toObject().value("attempts").toArray())
                if (a.toObject().value("term_resolved").toBool()) {
                    QJsonObject att = a.toObject();
                    att["chunk"] = o.toObject().value("chunk");
                    emit saveFeedbackRequested(att);
                    return;
                }
    });
    connect(m_reportToggle, &QPushButton::toggled, this, [this](bool on) {
        m_report->setVisible(on);
        if (on) m_report->setPlainText(reportText());
    });
    connect(m_list, &QListWidget::currentRowChanged, this, [this](int) { showIssue(); updateButtons(); });
    reset();
}

void QaPanel::setStatus(Status s) {
    m_status = s;
    m_badge->setText(kText[int(s)]);
    Fx::setProp(m_badge, "state", QString(kKeys[int(s)]));
    Fx::fadeIn(m_badge, 220);                      // short transition between verified / needs-review states
    m_badge->setToolTip(s == Status::Verified
                            ? "All automated checks passed. This is not a guarantee of correct pronunciation."
                            : s == Status::NeedsReview ? "Some segments could not be confirmed or look different from the text."
                            : s == Status::Failed ? "Verification could not be completed, or large parts of the speech do not match."
                            : s == Status::Checking ? "Recheck in progress" : "Recheck Speech has not been run for this audio.");
}

void QaPanel::reset() {
    m_summary = {};
    m_repair = {};
    m_candidate = false;
    m_list->clear();
    m_intended->clear();
    m_recognized->clear();
    m_reason->clear();
    m_report->clear();
    m_info->setText("Not checked yet. Use Recheck Speech to compare the audio with your text (optional).");
    setStatus(Status::NotChecked);
    updateButtons();
}

void QaPanel::setSessionAvailable(bool on) { m_session = on; updateButtons(); }
void QaPanel::setAsrAvailable(bool ok, const QString &why) {
    m_asr = ok;
    m_recheck->setToolTip(ok ? "Transcribe the generated audio with a speech recogniser and compare it with your text"
                             : "Speech recognition is not available: " + why);
    updateButtons();
}
void QaPanel::setLlmAvailable(bool on) { m_llm = on; updateButtons(); }
void QaPanel::setCandidateAvailable(bool on) { m_candidate = on; updateButtons(); }

void QaPanel::setBusy(bool busy, const QString &what) {
    if (busy && !m_busy) m_prevStatus = m_status;
    const bool was = m_busy;
    m_busy = busy;
    m_spin->setVisible(busy);
    if (busy) {
        setStatus(Status::Checking);
        m_info->setText(what.isEmpty() ? "Working…" : what);
    } else if (was && m_status == Status::Checking) {
        setStatus(m_prevStatus);                    // cancelled / failed job: show what was true before
        if (m_summary.isEmpty()) m_info->setText("Not checked yet. Use Recheck Speech to compare the audio with your text (optional).");
    }
    updateButtons();
}

int QaPanel::issueCount() const { return m_list->count(); }
int QaPanel::currentIssueRow() const { return m_list->currentRow(); }

QJsonObject QaPanel::currentIssue() const {
    auto *it = m_list->currentItem();
    return it ? it->data(Qt::UserRole).toJsonObject() : QJsonObject();
}

void QaPanel::refreshList() {
    m_list->clear();
    for (const auto &v : m_summary.value("issues").toArray()) {
        const QJsonObject i = v.toObject();
        if (i.value("severity").toString() != "review") continue;
        const QString what = i.value("intended").toString().isEmpty() ? i.value("type").toString().replace('_', ' ')
                                                                      : i.value("intended").toString();
        const QString heard = i.value("recognized").toString();
        auto *it = new QListWidgetItem(QString("%1  —  %2%3  (chunk %4)")
                                           .arg(what, i.value("type").toString().replace('_', ' '),
                                                heard.isEmpty() ? QString() : QString(", heard “%1”").arg(heard),
                                                QString::number(i.value("chunk").toInt() + 1)));
        it->setData(Qt::UserRole, i);
        it->setIcon(Icons::icon(i.value("type").toString() == "acoustic" ? "error" : "warning", "icon-accent"));
        m_list->addItem(it);
    }
    if (m_list->count()) m_list->setCurrentRow(0);
}

void QaPanel::showIssue() {
    const QJsonObject i = currentIssue();
    m_intended->clear();
    m_recognized->clear();
    m_reason->clear();
    if (i.isEmpty()) return;
    QString chunkText, recognized;
    for (const auto &c : m_summary.value("chunks").toArray())
        if (c.toObject().value("chunk").toInt() == i.value("chunk").toInt()) {
            chunkText = c.toObject().value("intended").toString();
            recognized = c.toObject().value("recognized").toString();
        }
    m_intended->setPlainText(chunkText);
    m_recognized->setPlainText(recognized.isEmpty() ? "(nothing recognised)" : recognized);
    const QJsonValue s = i.value("orig_start"), e = i.value("orig_end");
    if (!s.isNull() && !e.isNull()) {                 // highlight the suspect span, traced back to the source text
        QTextCursor cur(m_intended->document());
        cur.setPosition(qBound(0, s.toInt(), chunkText.size()));
        cur.setPosition(qBound(0, e.toInt(), chunkText.size()), QTextCursor::KeepAnchor);
        QTextEdit::ExtraSelection sel;
        sel.cursor = cur;
        sel.format.setBackground(QColor(229, 184, 74, 90));
        sel.format.setFontUnderline(true);
        m_intended->setExtraSelections({sel});
    } else {
        m_intended->setExtraSelections({});
    }
    QString r = i.value("reason").toString();
    if (!i.value("confidence").isNull()) r += QString("  Recogniser confidence: %1.").arg(pct(i.value("confidence")));
    if (i.value("sensitive").toBool()) r += "  (name, number or technical term)";
    m_reason->setText(r);
}

void QaPanel::updateButtons() {
    const bool showDetail = m_list->count() > 0 || m_candidate;
    if (showDetail != m_detail->isVisibleTo(this)) {
        m_detail->setVisible(showDetail);
        if (showDetail) Fx::fadeIn(m_detail, 220);
    }
    const bool hasIssue = !currentIssue().isEmpty();
    const bool repairable = hasIssue && currentIssue().value("severity").toString() == "review";
    const bool needsAttention = m_status == Status::NeedsReview || m_status == Status::Failed;
    m_recheck->setEnabled(m_session && m_asr && !m_busy);
    m_replay->setEnabled(m_session && hasIssue && !m_busy);
    m_correct->setEnabled(m_session && repairable && !m_busy);
    m_repairSel->setEnabled(m_session && repairable && !m_busy);
    m_repairAuto->setEnabled(m_session && needsAttention && !m_busy);
    m_accept->setEnabled(m_candidate && !m_busy);
    m_discard->setEnabled(m_candidate && !m_busy);
    m_accept->setVisible(m_candidate);
    m_discard->setVisible(m_candidate);
    bool anyResolved = false;
    for (const auto &o : m_repair.value("outcomes").toArray())
        for (const auto &a : o.toObject().value("attempts").toArray())
            anyResolved = anyResolved || a.toObject().value("term_resolved").toBool();
    m_saveFb->setVisible(anyResolved);
    m_saveFb->setEnabled(anyResolved && !m_busy);
    m_repairAuto->setToolTip(m_llm ? "Try approved corrections (dictionary and AI suggestions) on every flagged chunk, then recheck"
                                   : "Try approved corrections from the dictionary on every flagged chunk, then recheck "
                                     "(AI suggestions are turned off)");
}

void QaPanel::setSummary(const QJsonObject &s) {
    m_summary = s;
    setStatus(statusFromKey(s.value("status").toString()));
    refreshList();
    const QJsonObject cs = s.value("chunk_status").toObject();
    const QJsonObject asr = s.value("asr").toObject();
    m_info->setText(QString("%1 chunk(s) checked · %2 suspected issue(s) · recogniser %3/%4%5 · %6 s")
                        .arg(s.value("chunks_checked").toInt()).arg(s.value("issue_count").toInt())
                        .arg(asr.value("provider").toString(), asr.value("model").toString(),
                             s.value("mocked").toBool() ? " [MOCKED]" : "").arg(s.value("elapsed_s").toDouble(), 0, 'f', 1));
    if (m_reportToggle->isChecked()) m_report->setPlainText(reportText());
    updateButtons();
}

void QaPanel::setRepairResult(const QJsonObject &r) {
    m_repair = r;
    if (r.contains("summary") && !r.value("summary").isNull()) setSummary(r.value("summary").toObject());
    QStringList lines;
    for (const auto &ov : r.value("outcomes").toArray()) {
        const QJsonObject o = ov.toObject();
        lines << QString("Chunk %1: %2").arg(o.value("chunk").toInt() + 1, 0, 10).arg(o.value("status").toString());
        for (const auto &l : o.value("limitations").toArray()) lines << "   " + l.toString();
    }
    if (!lines.isEmpty()) m_reason->setText(lines.join('\n'));
    if (m_reportToggle->isChecked()) m_report->setPlainText(reportText());
    updateButtons();
}

QString QaPanel::reportText() const { return buildReport(m_summary, m_repair); }

QString QaPanel::buildReport(const QJsonObject &s, const QJsonObject &r) {
    if (s.isEmpty()) return "Not checked.";
    QStringList l;
    l << "Verification status: " + s.value("status").toString().replace('_', ' ')
      << QString("Chunks checked: %1 (verified %2, needs review %3, failed %4)")
             .arg(s.value("chunks_checked").toInt())
             .arg(s.value("chunk_status").toObject().value("verified").toInt())
             .arg(s.value("chunk_status").toObject().value("needs_review").toInt())
             .arg(s.value("chunk_status").toObject().value("failed").toInt())
      << QString("Suspected issues: %1 (plus %2 informational)").arg(s.value("issue_count").toInt()).arg(s.value("info_count").toInt());
    for (const auto &v : s.value("issues").toArray()) {
        const QJsonObject i = v.toObject();
        if (i.value("severity").toString() != "review") continue;
        l << QString("  • chunk %1 [%2] “%3” → “%4”  (recogniser confidence %5)")
                 .arg(i.value("chunk").toInt() + 1).arg(i.value("type").toString(), i.value("intended").toString(),
                                                        i.value("recognized").toString(), pct(i.value("confidence")));
    }
    l << QString("Mean recogniser word confidence: %1  (%2)").arg(pct(s.value("mean_word_confidence")), s.value("confidence_note").toString());
    const QJsonObject asr = s.value("asr").toObject();
    l << QString("Backend: %1 / voice %2 · recogniser %3 %4%5 · dictionary v%6 · %7 s")
             .arg(s.value("backend").toString(), s.value("voice").toString(), asr.value("provider").toString(),
                  asr.value("model").toString(), s.value("mocked").toBool() ? " (MOCKED, not real recognition)" : "",
                  QString::number(s.value("dictionary_version").toInt()), QString::number(s.value("elapsed_s").toDouble(), 'f', 1));
    if (!r.isEmpty()) {
        l << "" << "Corrections attempted:";
        for (const auto &ov : r.value("outcomes").toArray()) {
            const QJsonObject o = ov.toObject();
            for (const auto &av : o.value("attempts").toArray()) {
                const QJsonObject a = av.toObject();
                l << QString("  • chunk %1 “%2” with %3 → %4").arg(o.value("chunk").toInt() + 1).arg(a.value("term").toString(),
                        a.value("candidate").toObject().value("hint").toString().isEmpty()
                            ? "IPA " + a.value("candidate").toObject().value("ipa").toString()
                            : "“" + a.value("candidate").toObject().value("hint").toString() + "”",
                        a.value("term_resolved").toBool() ? "resolved (confirmed by recheck)" : "NOT resolved");
            }
            if (o.value("status").toString() == "unresolved")
                l << QString("  • chunk %1 remains unresolved").arg(o.value("chunk").toInt() + 1);
            for (const auto &lim : o.value("limitations").toArray()) l << "      limitation: " + lim.toString();
        }
    }
    l << "" << "Limitations:";
    for (const auto &x : s.value("limitations").toArray()) l << "  - " + x.toString();
    return l.join('\n');
}
