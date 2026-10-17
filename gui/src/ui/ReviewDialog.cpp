#include "ReviewDialog.h"

#include "Buttons.h"
#include "DictionaryDialog.h"
#include "Icons.h"
#include "ProtocolClient.h"

#include <QHBoxLayout>
#include <QHeaderView>
#include <QJsonArray>
#include <QLabel>
#include <QMessageBox>
#include <QTableWidget>
#include <QVBoxLayout>

namespace {
QTableWidget *table(const char *name, const QStringList &heads) {
    auto *t = new QTableWidget(0, heads.size());
    t->setObjectName(name);
    t->setHorizontalHeaderLabels(heads);
    t->horizontalHeader()->setStretchLastSection(true);
    t->verticalHeader()->hide();
    t->setSelectionBehavior(QAbstractItemView::SelectRows);
    t->setSelectionMode(QAbstractItemView::SingleSelection);
    t->setEditTriggers(QAbstractItemView::NoEditTriggers);
    t->setAlternatingRowColors(true);
    return t;
}
}  // namespace

ReviewDialog::ReviewDialog(ProtocolClient *client, const QJsonObject &analysis, const QString &text, const QString &language,
                           const QString &backend, const QJsonObject &llm, QWidget *parent)
    : QDialog(parent), m_client(client), m_text(text), m_language(language), m_backend(backend),
      m_ipa(analysis.value("controls").toObject().value("ipa").toBool()), m_llm(llm) {
    setWindowTitle("Review pronunciation");
    setObjectName("reviewDialog");
    resize(780, 520);
    auto *v = new QVBoxLayout(this);
    auto *intro = new QLabel(QString("%1 item(s) may need attention before speaking. Your text is not changed; add a pronunciation "
                                     "for anything that matters, or continue as is.")
                                 .arg(analysis.value("counts").toObject().value("review").toInt()));
    intro->setWordWrap(true);
    v->addWidget(intro);
    m_findings = table("reviewFindings", {"", "Kind", "Text", "Why / suggestion"});
    m_findings->setColumnWidth(0, 60);
    m_findings->setColumnWidth(1, 100);
    m_findings->setColumnWidth(2, 140);
    v->addWidget(m_findings, 3);
    for (const auto &fv : analysis.value("findings").toArray()) {
        const QJsonObject f = fv.toObject();
        const int r = m_findings->rowCount();
        m_findings->insertRow(r);
        QStringList sug;
        for (const auto &s : f.value("suggestions").toArray()) sug << s.toString();
        const QStringList cells{f.value("severity").toString() == "review" ? "review" : "info", f.value("kind").toString(),
                                f.value("text").toString() + (f.value("occurrences").toInt() > 1 ? QString("  ×%1").arg(f.value("occurrences").toInt()) : QString()),
                                f.value("reason").toString() + (sug.isEmpty() ? QString() : "  → " + sug.join(" / "))};
        for (int c = 0; c < cells.size(); ++c) {
            auto *it = new QTableWidgetItem(cells[c]);
            if (c == 0) it->setData(Qt::UserRole, f);
            m_findings->setItem(r, c, it);
        }
    }
    m_findings->sortItems(0, Qt::DescendingOrder);   // "review" before "info"

    auto *aiRow = new QHBoxLayout;
    auto *add = new AnimatedButton("Add pronunciation…");
    add->setObjectName("reviewAdd");
    add->setIcon(Icons::icon("correct"));
    auto *ai = new AnimatedButton("Suggest with AI");
    ai->setObjectName("reviewAi");
    ai->setIcon(Icons::icon("ai"));
    const bool llmOn = llm.value("enabled").toBool() && llm.value("available").toBool();
    ai->setEnabled(llmOn);
    ai->setToolTip(llmOn ? (llm.value("network").toBool() ? "Sends your text to the configured AI provider after you confirm"
                                                           : "Uses the configured local AI provider")
                         : "AI assistance is off or not configured (Settings > Pronunciation)");
    m_aiNote = new QLabel;
    m_aiNote->setObjectName("muted");
    m_aiNote->setProperty("small", true);
    m_aiNote->setWordWrap(true);
    aiRow->addWidget(add);
    aiRow->addWidget(ai);
    aiRow->addWidget(m_aiNote, 1);
    v->addLayout(aiRow);

    m_suggestions = table("reviewSuggestions", {"Word", "Respelling", "IPA", "Why (AI, unverified)"});
    m_suggestions->setMaximumHeight(130);
    m_suggestions->hide();
    v->addWidget(m_suggestions);
    auto *use = new AnimatedButton("Use selected suggestion…");
    use->setObjectName("reviewUse");
    use->hide();
    v->addWidget(use, 0, Qt::AlignLeft);

    auto *btns = new QHBoxLayout;
    btns->addStretch(1);
    auto *cancel = new AnimatedButton("Cancel");
    cancel->setObjectName("reviewCancel");
    auto *go = new AnimatedButton("Generate speech");
    go->setObjectName("reviewContinue");
    go->setIcon(Icons::icon("generate", "on-accent"));
    go->setProperty("variant", "primary");
    btns->addWidget(cancel);
    btns->addWidget(go);
    v->addLayout(btns);
    connect(add, &QPushButton::clicked, this, &ReviewDialog::addForRow);
    connect(ai, &QPushButton::clicked, this, &ReviewDialog::askAi);
    connect(use, &QPushButton::clicked, this, &ReviewDialog::useSuggestion);
    connect(m_suggestions, &QTableWidget::cellDoubleClicked, this, &ReviewDialog::useSuggestion);
    connect(cancel, &QPushButton::clicked, this, &QDialog::reject);
    connect(go, &QPushButton::clicked, this, &QDialog::accept);
}

int ReviewDialog::findingRows() const { return m_findings->rowCount(); }
int ReviewDialog::suggestionRows() const { return m_suggestions->rowCount(); }

void ReviewDialog::addForRow() {
    QJsonObject init{{"category", "name"}};
    if (auto *it = m_findings->item(m_findings->currentRow(), 0)) {
        const QJsonObject f = it->data(Qt::UserRole).toJsonObject();
        init["written"] = f.value("text").toString();
        const QString k = f.value("kind").toString();
        init["category"] = k == "acronym" ? "acronym" : k == "abbreviation" ? "abbreviation" : k == "technical" ? "technical"
                           : k == "name" ? "name" : "custom";
    }
    EntryEditor ed(init, m_ipa, false, this);
    if (ed.exec() != QDialog::Accepted) return;
    DictionaryDialog::addEntry(m_client, this, ed.entry(), m_backend, [this](bool stored, const QJsonObject &) {
        if (stored) m_aiNote->setText("Added to the dictionary; it will be used when you generate.");
    });
}

void ReviewDialog::askAi() {
    if (m_llm.value("network").toBool()) {
        const auto a = QMessageBox::question(this, "Send text to " + m_llm.value("provider").toString() + "?",
            QString("To get suggestions, up to %1 characters of your text are sent over the network to the “%2” AI provider "
                    "(model %3). Audio is never sent. Continue?")
                .arg(m_llm.value("max_input_chars").toInt()).arg(m_llm.value("provider").toString(), m_llm.value("model").toString()));
        if (a != QMessageBox::Yes) return;
    }
    m_aiNote->setText("Asking the AI…");
    m_client->call("llm_analyze", {{"text", m_text}, {"language", m_language}, {"backend", m_backend},
                                   {"allow_network", m_llm.value("network").toBool()}}, this,
                   [this](const QString &st, const QJsonObject &r) {
        if (st != "success") {
            m_aiNote->setText(QString("AI suggestions unavailable (%1): %2").arg(r.value("code").toString(), r.value("message").toString()));
            return;
        }
        applySuggestions(r);
    }, 120000);
}

void ReviewDialog::applySuggestions(const QJsonObject &r) {
    m_suggestions->setRowCount(0);
    for (const auto &sv : r.value("suggestions").toArray()) {
        const QJsonObject s = sv.toObject();
        const int row = m_suggestions->rowCount();
        m_suggestions->insertRow(row);
        const QStringList cells{s.value("word").toString(), s.value("respelling").toString(), s.value("ipa").toString(), s.value("reason").toString()};
        for (int c = 0; c < cells.size(); ++c) {
            auto *it = new QTableWidgetItem(cells[c]);
            if (c == 0) it->setData(Qt::UserRole, s);
            m_suggestions->setItem(row, c, it);
        }
    }
    const int rej = r.value("rejected").toArray().size();
    m_aiNote->setText(QString("%1 suggestion(s) passed validation%2. They are unverified: nothing is applied until you add them.")
                          .arg(m_suggestions->rowCount()).arg(rej ? QString(", %1 rejected as unsafe or not in your text").arg(rej) : QString()));
    m_suggestions->setVisible(m_suggestions->rowCount() > 0);
    if (auto *u = findChild<QPushButton *>("reviewUse")) u->setVisible(m_suggestions->rowCount() > 0);
}

void ReviewDialog::useSuggestion() {
    auto *it = m_suggestions->item(m_suggestions->currentRow(), 0);
    if (!it) return;
    const QJsonObject s = it->data(Qt::UserRole).toJsonObject();
    const QString k = s.value("kind").toString();
    QJsonObject init{{"written", s.value("word")}, {"hint", s.value("respelling")}, {"ipa", s.value("ipa")},
                     {"category", k == "acronym" ? "acronym" : k == "abbreviation" ? "abbreviation" : k == "technical" ? "technical"
                                  : k == "name" ? "name" : "custom"},
                     {"note", "suggested by AI, approved by you"}};
    EntryEditor ed(init, m_ipa, false, this);
    if (ed.exec() != QDialog::Accepted) return;
    QJsonObject e = ed.entry();
    e["source"] = "llm-approved";
    DictionaryDialog::addEntry(m_client, this, e, m_backend, [this](bool stored, const QJsonObject &) {
        if (stored) m_aiNote->setText("Added to the dictionary.");
    });
}
