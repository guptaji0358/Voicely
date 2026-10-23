#include "PronSettings.h"
#include "Controls.h"

#include "Buttons.h"
#include "DialogFrame.h"
#include "DictionaryDialog.h"
#include "Icons.h"
#include "ProtocolClient.h"

#include <QCheckBox>
#include <QComboBox>
#include <QDateTime>
#include <QDialog>
#include <QFileDialog>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QJsonArray>
#include <QLabel>
#include <QLineEdit>
#include <QMessageBox>
#include <QTableWidget>
#include <QVBoxLayout>

namespace {
QLabel *note(const QString &t = {}) {
    auto *l = new QLabel(t);
    l->setObjectName("muted");
    l->setProperty("small", true);
    l->setWordWrap(true);
    return l;
}
AnimatedButton *button(const QString &t, const char *icon, const char *name) {
    auto *b = new AnimatedButton(t);
    b->setObjectName(name);
    b->setIcon(Icons::icon(icon));
    return b;
}
}  // namespace

PronSettings::PronSettings(ProtocolClient *client, const QString &backend, bool ipa, QWidget *parent)
    : QWidget(parent), m_client(client), m_backend(backend), m_ipa(ipa) {
    setObjectName("pronSettings");
    auto *v = new QVBoxLayout(this);
    v->setSpacing(10);

    auto *asr = new QGroupBox("Speech recognition (for Recheck Speech)");
    auto *al = new QVBoxLayout(asr);
    m_asrStatus = note();
    m_download = button("Download recognition model…", "import", "asrDownload");
    m_download->setEnabled(false);
    al->addWidget(m_asrStatus);
    al->addWidget(m_download, 0, Qt::AlignLeft);
    al->addWidget(note("The model runs offline on this computer. Downloading it needs the network and your confirmation; "
                       "no document text or audio is sent anywhere."));
    v->addWidget(asr);

    auto *llm = new QGroupBox("AI pronunciation assistance (optional)");
    auto *ll = new QVBoxLayout(llm);
    m_llmOn = new FancyCheckBox("Enable AI suggestions for ambiguous words");
    m_llmOn->setObjectName("llmEnable");
    m_provider = new QComboBox;
    m_provider->setObjectName("llmProvider");
    m_provider->addItem("Google Gemini (sends text to Google)", "google");
    m_model = new QLineEdit;
    m_model->setObjectName("llmModel");
    m_model->setPlaceholderText("model name");
    m_examples = new FancyCheckBox("Include my approved corrections as examples in the request");
    m_llmStatus = note();
    ll->addWidget(m_llmOn);
    auto *row = new QHBoxLayout;
    row->addWidget(m_provider, 2);
    row->addWidget(m_model, 1);
    ll->addLayout(row);
    ll->addWidget(m_examples);
    ll->addWidget(m_llmStatus);
    ll->addWidget(note("Off by default. Speech generation never depends on it. Each request asks for your confirmation before text "
                       "is sent. The API key is read from the GEMINI_API_KEY environment variable (or the Settings field for this "
                       "session) and is never stored. Suggestions are validated and only applied after you approve them. "
                       "The model is not trained on your text."));
    v->addWidget(llm);

    auto *fb = new QGroupBox("Learning from my corrections (local)");
    auto *fl = new QVBoxLayout(fb);
    m_fbOn = new FancyCheckBox("Save corrections I approve on this computer");
    m_fbOn->setObjectName("fbConsent");
    m_fbAudio = new FancyCheckBox("Also keep short audio samples of those corrections");
    m_fbAudio->setObjectName("fbAudioConsent");
    m_fbStatus = note();
    auto *fr = new QHBoxLayout;
    auto *view = button("View…", "info", "fbView");
    auto *exp = button("Export…", "export-list", "fbExport");
    auto *del = button("Delete all", "delete", "fbDeleteAll");
    auto *dict = button("Dictionary…", "dictionary", "pronDictionary");
    fr->addWidget(view);
    fr->addWidget(exp);
    fr->addWidget(del);
    fr->addStretch(1);
    fr->addWidget(dict);
    fl->addWidget(m_fbOn);
    fl->addWidget(m_fbAudio);
    fl->addWidget(m_fbStatus);
    fl->addLayout(fr);
    fl->addWidget(note("Corrections become dictionary entries and regression tests that guard future changes. Nothing is uploaded and no "
                       "AI model is trained or fine-tuned automatically."));
    v->addWidget(fb);
    v->addStretch(1);

    connect(m_download, &QPushButton::clicked, this, [this] {
        if (Msg::question(this, "Download recognition model",
                "This downloads the speech-recognition model (about 150 MB) from Hugging Face. Only the model download uses the "
                "network; none of your text or audio is sent. Continue?") != QMessageBox::Yes) return;
        m_download->setEnabled(false);
        m_asrStatus->setText("Downloading the model… (this can take a few minutes)");
        m_client->call("asr_download", {{"confirm", true}}, this, [this](const QString &st, const QJsonObject &r) {
            if (st != "success") Msg::warning(this, "Download failed", r.value("message").toString());
            refresh();
            emit changed();
        }, 30 * 60 * 1000);
    });
    auto pushLlm = [this] {
        m_client->call("llm_configure", {{"settings", QJsonObject{{"enabled", m_llmOn->isChecked()},
                                                                  {"provider", m_provider->currentData().toString()},
                                                                  {"model", m_model->text().trimmed()},
                                                                  {"use_examples", m_examples->isChecked()}}}},
                       this, [this](const QString &st, const QJsonObject &r) {
            if (st != "success") Msg::warning(this, "AI settings", r.value("message").toString());
            refresh();
            emit changed();
        });
    };
    connect(m_llmOn, &QCheckBox::clicked, this, pushLlm);
    connect(m_examples, &QCheckBox::clicked, this, [this, pushLlm] {
        if (m_examples->isChecked() && Msg::question(this, "Send examples?",
                "Your approved corrections will be included in each AI request, so they are sent to the provider too. Enable?") != QMessageBox::Yes) {
            m_examples->setChecked(false);
            return;
        }
        pushLlm();
    });
    connect(m_model, &QLineEdit::editingFinished, this, pushLlm);
    auto pushConsent = [this] {
        m_client->call("feedback", {{"op", "set_consent"}, {"store_feedback", m_fbOn->isChecked()},
                                    {"store_audio", m_fbAudio->isChecked() && m_fbOn->isChecked()}}, this,
                       [this](const QString &, const QJsonObject &) { refresh(); });
    };
    connect(m_fbOn, &QCheckBox::clicked, this, pushConsent);
    connect(m_fbAudio, &QCheckBox::clicked, this, pushConsent);
    connect(dict, &QPushButton::clicked, this, [this] { DictionaryDialog(m_client, m_backend, m_ipa, this).exec(); });
    connect(exp, &QPushButton::clicked, this, [this] {
        const QString d = QFileDialog::getExistingDirectory(this, "Export feedback to folder");
        if (d.isEmpty()) return;
        m_client->call("feedback", {{"op", "export"}, {"dest_dir", d}}, this, [this](const QString &st, const QJsonObject &r) {
            Msg::information(this, "Export", st == "success" ? QString("Exported %1 record(s) to\n%2").arg(r.value("records").toInt()).arg(r.value("path").toString())
                                                                     : r.value("message").toString());
        });
    });
    connect(del, &QPushButton::clicked, this, [this] {
        if (Msg::question(this, "Delete all feedback", "Delete every saved correction record and audio sample? This cannot be undone.") != QMessageBox::Yes) return;
        m_client->call("feedback", {{"op", "delete_all"}}, this, [this](const QString &, const QJsonObject &) { refresh(); });
    });
    connect(view, &QPushButton::clicked, this, [this] {
        m_client->call("feedback", {{"op", "list"}}, this, [this](const QString &st, const QJsonObject &r) {
            if (st != "success") return;
            StyledDialog d(this, "save-feedback");
            d.setWindowTitle("Saved corrections");
            d.resize(700, 360);
            auto *l = new QVBoxLayout(&d);
            l->setContentsMargins(14, 4, 14, 10);
            auto *t = new QTableWidget(0, 5);
            t->setHorizontalHeaderLabels({"Date", "Word", "Read as", "Worked", "Audio"});
            t->horizontalHeader()->setStretchLastSection(true);
            t->verticalHeader()->hide();
            t->setSelectionBehavior(QAbstractItemView::SelectRows);
            t->setEditTriggers(QAbstractItemView::NoEditTriggers);
            for (const auto &rv : r.value("records").toArray()) {
                const QJsonObject rec = rv.toObject();
                const int row = t->rowCount();
                t->insertRow(row);
                const QJsonObject c = rec.value("correction").toObject();
                const QStringList cells{QDateTime::fromSecsSinceEpoch(qint64(rec.value("created").toDouble())).toString("yyyy-MM-dd HH:mm"),
                                        rec.value("term").toString(), c.value("hint").toString() + (c.value("ipa").toString().isEmpty() ? "" : " /" + c.value("ipa").toString() + "/"),
                                        rec.value("succeeded").isNull() ? "unknown" : rec.value("succeeded").toBool() ? "yes" : "no",
                                        rec.value("audio").isNull() ? "—" : "kept"};
                for (int i = 0; i < cells.size(); ++i) {
                    auto *it = new QTableWidgetItem(cells[i]);
                    if (i == 0) it->setData(Qt::UserRole, rec.value("id").toString());
                    t->setItem(row, i, it);
                }
            }
            l->addWidget(t);
            auto *hl = new QHBoxLayout;
            auto *delOne = button("Delete selected", "delete", "fbDeleteOne");
            auto *close = button("Close", "cancel", "fbClose");
            hl->addWidget(delOne);
            hl->addStretch(1);
            hl->addWidget(close);
            l->addLayout(hl);
            connect(close, &QPushButton::clicked, &d, &QDialog::accept);
            connect(delOne, &QPushButton::clicked, &d, [this, t] {
                auto *it = t->item(t->currentRow(), 0);
                if (!it) return;
                m_client->call("feedback", {{"op", "delete"}, {"id", it->data(Qt::UserRole).toString()}}, this, [t](const QString &, const QJsonObject &) {});
                t->removeRow(t->currentRow());
            });
            d.exec();
            refresh();
        });
    });
    refresh();
}

void PronSettings::refresh() {
    m_client->call("asr_status", {}, this, [this](const QString &st, const QJsonObject &r) {
        if (st != "success") { m_asrStatus->setText(r.value("message").toString()); return; }
        const bool ok = r.value("available").toBool();
        QStringList p;
        for (const auto &x : r.value("problems").toArray()) p << x.toString();
        m_asrStatus->setText(ok ? QString("Ready: %1 (%2)").arg(r.value("provider").toString(), r.value("model").toString())
                                : "Not available: " + p.join(" "));
        const bool canDownload = !ok && p.join(' ').contains("not downloaded");
        m_download->setEnabled(canDownload);
    });
    m_client->call("llm_status", {}, this, [this](const QString &st, const QJsonObject &r) {
        if (st != "success") return;
        m_llmOn->setChecked(r.value("enabled").toBool());
        m_examples->setChecked(r.value("use_examples").toBool());
        m_model->setText(r.value("model").toString());
        QStringList p;
        for (const auto &x : r.value("problems").toArray()) p << x.toString();
        m_llmStatus->setText(!r.value("enabled").toBool() ? "Off."
                             : r.value("available").toBool() ? QString("Ready: %1, model %2 (network: %3).")
                                                                  .arg(r.value("provider").toString(), r.value("model").toString(),
                                                                       r.value("network").toBool() ? "yes" : "no")
                                                             : "Not ready: " + p.join(" "));
    });
    m_client->call("feedback", {{"op", "status"}}, this, [this](const QString &st, const QJsonObject &r) {
        if (st != "success") return;
        const QJsonObject c = r.value("consent").toObject();
        m_fbOn->setChecked(c.value("store_feedback").toBool());
        m_fbAudio->setChecked(c.value("store_audio").toBool());
        m_fbAudio->setEnabled(c.value("store_feedback").toBool());
        m_fbStatus->setText(QString("%1 saved correction(s) · %2 audio sample(s) · stored in %3")
                                .arg(r.value("records").toInt()).arg(r.value("audio").toInt()).arg(r.value("location").toString()));
    });
}
