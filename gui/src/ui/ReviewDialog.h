#pragma once
// Pre-synthesis review: lists the words/phrases the analysis flagged (names, numbers, acronyms, heteronyms,
// multilingual text...) and lets the user add pronunciations - by hand or from validated AI suggestions - before
// generating. Nothing is changed in the text; accepting the dialog only means "go ahead and generate".
#include "DialogFrame.h"
#include <QJsonObject>

class ProtocolClient;
class QLabel;
class QTableWidget;

class ReviewDialog : public StyledDialog {
    Q_OBJECT
public:
    ReviewDialog(ProtocolClient *client, const QJsonObject &analysis, const QString &text, const QString &language,
                 const QString &backend, const QJsonObject &llmStatus, QWidget *parent = nullptr);
    int findingRows() const;
    int suggestionRows() const;
    void applySuggestions(const QJsonObject &llmResult);   // also used by tests

private:
    void addForRow();
    void askAi();
    void useSuggestion();
    ProtocolClient *m_client;
    QString m_text, m_language, m_backend;
    bool m_ipa;
    QJsonObject m_llm;
    QTableWidget *m_findings, *m_suggestions;
    QLabel *m_aiNote;
};
