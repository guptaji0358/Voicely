#include "DictionaryDialog.h"

#include "Buttons.h"
#include "Icons.h"
#include "ProtocolClient.h"

#include <QCheckBox>
#include <QComboBox>
#include <QDialogButtonBox>
#include <QFileDialog>
#include <QFormLayout>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QJsonArray>
#include <QLabel>
#include <QLineEdit>
#include <QMessageBox>
#include <QTableWidget>
#include <QVBoxLayout>
#include <functional>
#include <memory>

namespace {
const char *kLangs[][2] = {{"*", "Any language"}, {"en-us", "English (US)"}, {"en-gb", "English (UK)"}, {"fr-fr", "French"},
                           {"es", "Spanish"}, {"hi", "Hindi"}, {"it", "Italian"}, {"pt-br", "Portuguese (BR)"},
                           {"ja", "Japanese"}, {"cmn", "Mandarin"}};
const char *kCats[] = {"name", "technical", "acronym", "abbreviation", "custom", "correction"};

QString words(const QJsonValue &v) {
    QStringList l;
    for (const auto &x : v.toArray()) l << x.toString();
    return l.join(' ');
}

QString regressionText(const QJsonObject &gate) {
    QStringList l;
    for (const auto &f : gate.value("failures").toArray()) l << "• " + f.toObject().value("why").toString();
    return l.join('\n');
}
}  // namespace

// --------------------------------------------------------------------------------------------- EntryEditor
EntryEditor::EntryEditor(const QJsonObject &init, bool ipaSupported, bool repairMode, QWidget *parent) : QDialog(parent) {
    setWindowTitle(init.contains("id") ? "Edit pronunciation" : "Add pronunciation");
    setObjectName("entryEditor");
    setMinimumWidth(460);
    auto *f = new QFormLayout(this);
    m_written = new QLineEdit(init.value("written").toString());
    m_written->setObjectName("entryWritten");
    m_hint = new QLineEdit(init.value("hint").toString());
    m_hint->setObjectName("entryHint");
    m_hint->setPlaceholderText("e.g. win  (how it should be read, in plain letters)");
    m_ipa = new QLineEdit(init.value("ipa").toString());
    m_ipa->setObjectName("entryIpa");
    m_ipa->setPlaceholderText(ipaSupported ? "optional IPA, e.g. wˈɪn" : "optional (this backend cannot use IPA)");
    m_correction = new QLineEdit(init.value("correction").toString());
    m_correction->setObjectName("entryCorrection");
    m_correction->setPlaceholderText("optional approved replacement phrase");
    m_lang = new QComboBox;
    for (auto &l : kLangs) m_lang->addItem(l[1], l[0]);
    const int li = m_lang->findData(init.value("language").toString("*"));
    m_lang->setCurrentIndex(li < 0 ? 0 : li);
    m_category = new QComboBox;
    for (auto *c : kCats) m_category->addItem(c);
    m_category->setCurrentText(init.value("category").toString("name"));
    m_case = new QCheckBox("Match exact capitalisation");
    m_case->setChecked(init.value("case_sensitive").toBool());
    m_before = new QLineEdit(words(init.value("context_before")));
    m_before->setPlaceholderText("only when preceded by one of these words (optional)");
    m_after = new QLineEdit(words(init.value("context_after")));
    m_after->setPlaceholderText("only when followed by one of these words (optional)");
    m_note = new QLineEdit(init.value("note").toString());
    m_ipaNote = new QLabel(ipaSupported ? "IPA is passed to the local voice exactly." :
                                          "IPA is stored but ignored by this backend; the respelling is used instead.");
    m_ipaNote->setObjectName("muted");
    m_ipaNote->setProperty("small", true);
    m_ipaNote->setWordWrap(true);
    f->addRow("Written form", m_written);
    f->addRow("Read as (respelling)", m_hint);
    f->addRow("IPA", m_ipa);
    f->addRow("", m_ipaNote);
    f->addRow("Approved replacement", m_correction);
    f->addRow("Language", m_lang);
    f->addRow("Category", m_category);
    f->addRow("", m_case);
    f->addRow("Only when preceded by", m_before);
    f->addRow("Only when followed by", m_after);
    f->addRow("Note", m_note);
    m_save = new QCheckBox("Also save in the pronunciation dictionary (otherwise use for this repair only)");
    m_save->setChecked(true);
    m_save->setVisible(repairMode);
    f->addRow("", m_save);
    auto *bb = new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel);
    f->addRow(bb);
    connect(bb, &QDialogButtonBox::accepted, this, [this] {
        if (m_written->text().trimmed().isEmpty() ||
            (m_hint->text().trimmed().isEmpty() && m_ipa->text().trimmed().isEmpty() && m_correction->text().trimmed().isEmpty())) {
            QMessageBox::warning(this, "Pronunciation", "Enter the written form and at least a respelling, IPA or replacement.");
            return;
        }
        accept();
    });
    connect(bb, &QDialogButtonBox::rejected, this, &QDialog::reject);
}

bool EntryEditor::saveToDictionary() const { return m_save->isVisibleTo(const_cast<EntryEditor *>(this)) ? m_save->isChecked() : true; }

QJsonObject EntryEditor::entry() const {
    return {{"written", m_written->text().trimmed()}, {"hint", m_hint->text().trimmed()}, {"ipa", m_ipa->text().trimmed()},
            {"correction", m_correction->text().trimmed()}, {"language", m_lang->currentData().toString()},
            {"category", m_category->currentText()}, {"case_sensitive", m_case->isChecked()},
            {"context_before", m_before->text().trimmed()}, {"context_after", m_after->text().trimmed()},
            {"note", m_note->text().trimmed()}};
}

// ------------------------------------------------------------------------------------------ DictionaryDialog
DictionaryDialog::DictionaryDialog(ProtocolClient *client, const QString &backend, bool ipaSupported, QWidget *parent)
    : QDialog(parent), m_client(client), m_backend(backend), m_ipa(ipaSupported) {
    setWindowTitle("Pronunciation dictionary");
    setObjectName("dictionaryDialog");
    resize(760, 460);
    auto *v = new QVBoxLayout(this);
    auto *top = new QHBoxLayout;
    m_search = new QLineEdit;
    m_search->setObjectName("dictSearch");
    m_search->setPlaceholderText("Search written form, respelling or note…");
    m_search->setClearButtonEnabled(true);
    m_version = new QLabel;
    m_version->setObjectName("muted");
    top->addWidget(m_search, 1);
    top->addWidget(m_version);
    v->addLayout(top);
    m_table = new QTableWidget(0, 6);
    m_table->setObjectName("dictTable");
    m_table->setHorizontalHeaderLabels({"Written", "Language", "Read as", "IPA", "Category", "Context"});
    m_table->horizontalHeader()->setStretchLastSection(true);
    m_table->horizontalHeader()->setSectionResizeMode(QHeaderView::Interactive);
    m_table->setSelectionBehavior(QAbstractItemView::SelectRows);
    m_table->setSelectionMode(QAbstractItemView::SingleSelection);
    m_table->setEditTriggers(QAbstractItemView::NoEditTriggers);
    m_table->verticalHeader()->hide();
    m_table->setAlternatingRowColors(true);
    v->addWidget(m_table, 1);
    auto *row = new QHBoxLayout;
    auto mk = [&](const QString &t, const char *icon, const char *name) {
        auto *b = new AnimatedButton(t);
        b->setObjectName(name);
        b->setIcon(Icons::icon(icon));
        row->addWidget(b);
        return b;
    };
    auto *add = mk("Add…", "add", "dictAdd");
    auto *edit = mk("Edit…", "correct", "dictEdit");
    auto *del = mk("Delete", "delete", "dictDelete");
    row->addStretch(1);
    auto *imp = mk("Import…", "import-list", "dictImport");
    auto *exp = mk("Export…", "export-list", "dictExport");
    auto *close = mk("Close", "cancel", "dictClose");
    v->addLayout(row);
    connect(add, &QPushButton::clicked, this, [this] { editRow(true); });
    connect(edit, &QPushButton::clicked, this, [this] { editRow(false); });
    connect(m_table, &QTableWidget::cellDoubleClicked, this, [this] { editRow(false); });
    connect(del, &QPushButton::clicked, this, &DictionaryDialog::deleteRow);
    connect(imp, &QPushButton::clicked, this, &DictionaryDialog::importFile);
    connect(exp, &QPushButton::clicked, this, &DictionaryDialog::exportFile);
    connect(close, &QPushButton::clicked, this, &QDialog::accept);
    connect(m_search, &QLineEdit::textChanged, this, [this] { reload(); });
    reload();
}

int DictionaryDialog::rowCount() const { return m_table->rowCount(); }

void DictionaryDialog::reload() {
    m_client->call("dictionary", {{"op", "search"}, {"query", m_search->text()}, {"backend", m_backend}}, this,
                   [this](const QString &st, const QJsonObject &r) {
        if (st != "success") {
            m_version->setText("Error: " + r.value("message").toString());
            return;
        }
        const QJsonArray es = r.value("entries").toArray();
        m_table->setRowCount(es.size());
        for (int i = 0; i < es.size(); ++i) {
            const QJsonObject e = es[i].toObject();
            const QString ctx = (words(e.value("context_before")).isEmpty() ? QString() : "after: " + words(e.value("context_before")) + " ")
                                + (words(e.value("context_after")).isEmpty() ? QString() : "before: " + words(e.value("context_after")));
            const QStringList cells{e.value("written").toString(), e.value("language").toString(),
                                    e.value("correction").toString().isEmpty() ? e.value("hint").toString() : e.value("correction").toString(),
                                    e.value("ipa").toString(), e.value("category").toString(), ctx};
            for (int c = 0; c < cells.size(); ++c) {
                auto *it = new QTableWidgetItem(cells[c]);
                if (c == 0) it->setData(Qt::UserRole, e);
                m_table->setItem(i, c, it);
            }
        }
        m_version->setText(QString("%1 entries · version %2").arg(r.value("count").toInt()).arg(r.value("version").toInt()));
    });
}

void DictionaryDialog::addEntry(ProtocolClient *client, QWidget *ctx, const QJsonObject &entry, const QString &backend,
                                std::function<void(bool, const QJsonObject &)> done) {
    auto send = std::make_shared<std::function<void(bool)>>();
    *send = [=](bool check) {
        client->call("dictionary", {{"op", "add"}, {"entry", entry}, {"backend", backend}, {"check_regression", check}}, ctx,
                     [=](const QString &st, const QJsonObject &r) {
            if (st != "success") {
                QMessageBox::warning(ctx, "Pronunciation dictionary", r.value("message").toString());
                done(false, r);
                return;
            }
            if (!r.value("added").toBool()) {
                const auto a = QMessageBox::question(ctx, "This change breaks approved corrections",
                    "Adding this entry would regress corrections you approved earlier:\n\n" +
                    regressionText(r.value("regression").toObject()) + "\n\nAdd it anyway?");
                if (a == QMessageBox::Yes) (*send)(false);
                else done(false, r);
                return;
            }
            done(true, r);
        });
    };
    (*send)(true);
}

void DictionaryDialog::editRow(bool add) {
    QJsonObject init;
    if (!add) {
        auto *it = m_table->item(m_table->currentRow(), 0);
        if (!it) return;
        init = it->data(Qt::UserRole).toJsonObject();
    }
    EntryEditor ed(init, m_ipa, false, this);
    if (ed.exec() != QDialog::Accepted) return;
    if (add) {
        addEntry(m_client, this, ed.entry(), m_backend, [this](bool, const QJsonObject &) { reload(); });
        return;
    }
    const QString id = init.value("id").toString();
    auto send = std::make_shared<std::function<void(bool)>>();
    const QJsonObject e = ed.entry();
    *send = [=](bool check) {
        m_client->call("dictionary", {{"op", "update"}, {"id", id}, {"entry", e}, {"backend", m_backend}, {"check_regression", check}},
                       this, [=](const QString &st, const QJsonObject &r) {
            if (st != "success") {
                QMessageBox::warning(this, "Pronunciation dictionary", r.value("message").toString());
            } else if (!r.value("updated").toBool()) {
                if (QMessageBox::question(this, "This change breaks approved corrections",
                        regressionText(r.value("regression").toObject()) + "\n\nSave it anyway?") == QMessageBox::Yes)
                    (*send)(false);
            }
            reload();
        });
    };
    (*send)(true);
}

void DictionaryDialog::deleteRow() {
    auto *it = m_table->item(m_table->currentRow(), 0);
    if (!it) return;
    const QJsonObject e = it->data(Qt::UserRole).toJsonObject();
    if (QMessageBox::question(this, "Delete", QString("Delete the entry for “%1”?").arg(e.value("written").toString())) != QMessageBox::Yes)
        return;
    auto send = std::make_shared<std::function<void(bool)>>();
    *send = [=](bool force) {
        m_client->call("dictionary", {{"op", "delete"}, {"id", e.value("id")}, {"force", force}}, this,
                       [=](const QString &st, const QJsonObject &r) {
            if (st == "success" && !r.value("deleted").toBool()) {
                if (QMessageBox::question(this, "This removes an approved correction",
                        regressionText(r.value("regression").toObject()) + "\n\nDelete it anyway?") == QMessageBox::Yes)
                    (*send)(true);
            }
            reload();
        });
    };
    (*send)(false);
}

void DictionaryDialog::importFile() {
    const QString f = QFileDialog::getOpenFileName(this, "Import dictionary", {}, "Dictionary (*.json *.csv)");
    if (f.isEmpty()) return;
    const QString fmt = f.endsWith(".csv", Qt::CaseInsensitive) ? "csv" : "json";
    m_client->call("dictionary", {{"op", "import"}, {"path", f}, {"format", fmt}, {"backend", m_backend}}, this,
                   [this](const QString &st, const QJsonObject &r) {
        if (st != "success") QMessageBox::warning(this, "Import failed", r.value("message").toString() + "\n\nNothing was imported.");
        else QMessageBox::information(this, "Import", QString("Added %1, updated %2, skipped %3 existing.")
                                                          .arg(r.value("added").toInt()).arg(r.value("updated").toInt()).arg(r.value("skipped").toInt()));
        reload();
    });
}

void DictionaryDialog::exportFile() {
    QString sel;
    const QString f = QFileDialog::getSaveFileName(this, "Export dictionary", "pronunciation_dictionary.json",
                                                   "JSON (*.json);;CSV (*.csv)", &sel);
    if (f.isEmpty()) return;
    const QString fmt = f.endsWith(".csv", Qt::CaseInsensitive) ? "csv" : "json";
    m_client->call("dictionary", {{"op", "export"}, {"path", f}, {"format", fmt}}, this, [this](const QString &st, const QJsonObject &r) {
        if (st != "success") QMessageBox::warning(this, "Export failed", r.value("message").toString());
    });
}
