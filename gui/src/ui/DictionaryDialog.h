#pragma once
// Pronunciation dictionary editor: search, add, edit, delete, import, export. All persistence is in the Python
// worker (dictionary action); this dialog never edits files itself.
#include "DialogFrame.h"
#include <QJsonObject>
#include <functional>

class AnimatedButton;
class ProtocolClient;
class QCheckBox;
class QComboBox;
class QLabel;
class QLineEdit;
class QTableWidget;

class EntryEditor : public StyledDialog {
    Q_OBJECT
public:
    // initial: any subset of the entry fields. repairMode adds the "save in dictionary" choice.
    EntryEditor(const QJsonObject &initial, bool ipaSupported, bool repairMode, QWidget *parent = nullptr);
    QJsonObject entry() const;
    bool saveToDictionary() const;

private:
    QLineEdit *m_written, *m_hint, *m_ipa, *m_correction, *m_before, *m_after, *m_note;
    QComboBox *m_lang, *m_category;
    QCheckBox *m_case, *m_save;
    QLabel *m_ipaNote;
};

class DictionaryDialog : public StyledDialog {
    Q_OBJECT
public:
    DictionaryDialog(ProtocolClient *client, const QString &backend, bool ipaSupported, QWidget *parent = nullptr);
    int rowCount() const;

    // Adds an entry through the worker (regression-gated). `done` receives whether it was stored.
    static void addEntry(ProtocolClient *client, QWidget *ctx, const QJsonObject &entry, const QString &backend,
                         std::function<void(bool stored, const QJsonObject &response)> done);

private:
    void reload();
    void editRow(bool add);
    void deleteRow();
    void importFile();
    void exportFile();
    ProtocolClient *m_client;
    QString m_backend;
    bool m_ipa;
    QLineEdit *m_search;
    QTableWidget *m_table;
    QLabel *m_version;
};
