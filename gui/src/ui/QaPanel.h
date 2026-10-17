#pragma once
// Pronunciation QA results panel: Recheck Speech, verification status, suspected problems with the intended and
// recognised text side by side, replay of the affected segment, correction / repair / accept controls and a report.
// It only displays data it is given and emits requests; MainWindow talks to the worker.
#include <QJsonArray>
#include <QJsonObject>
#include <QWidget>

class AnimatedButton;
class QLabel;
class QListWidget;
class QPlainTextEdit;
class StateIcon;

class QaPanel : public QWidget {
    Q_OBJECT
public:
    enum class Status { NotChecked, Checking, Verified, NeedsReview, Failed };
    explicit QaPanel(QWidget *parent = nullptr);

    Status status() const { return m_status; }
    void setStatus(Status s);
    static QString statusKey(Status s);            // "not_checked" ... "failed"
    static Status statusFromKey(const QString &k);

    void reset();                                  // new audio: nothing has been checked
    void setSessionAvailable(bool on);             // enables the controls that need a generated session
    void setBusy(bool busy, const QString &what = {});
    void setSummary(const QJsonObject &summary);   // result of recheck / repaired mix
    void setRepairResult(const QJsonObject &result);
    void setCandidateAvailable(bool on);
    void setAsrAvailable(bool ok, const QString &why);
    void setLlmAvailable(bool on);

    QJsonObject summary() const { return m_summary; }
    int issueCount() const;
    int currentIssueRow() const;
    QJsonObject currentIssue() const;
    QListWidget *issueList() const { return m_list; }
    QString reportText() const;
    static QString buildReport(const QJsonObject &summary, const QJsonObject &repair);

signals:
    void recheckRequested();
    void replayRequested(int chunk);
    void correctRequested(const QJsonObject &issue);
    void repairRequested(const QString &mode, const QJsonArray &chunks);
    void acceptRepairRequested();
    void discardRepairRequested();
    void saveFeedbackRequested(const QJsonObject &attempt);
    void dictionaryRequested();

private:
    void refreshList();
    void showIssue();
    void updateButtons();

    Status m_status = Status::NotChecked, m_prevStatus = Status::NotChecked;
    QJsonObject m_summary, m_repair;
    bool m_session = false, m_busy = false, m_candidate = false, m_asr = true, m_llm = false;
    QLabel *m_badge, *m_info, *m_reason, *m_note;
    QWidget *m_detail;
    StateIcon *m_spin;
    QListWidget *m_list;
    QPlainTextEdit *m_intended, *m_recognized, *m_report;
    AnimatedButton *m_recheck, *m_replay, *m_correct, *m_repairSel, *m_repairAuto, *m_accept, *m_discard, *m_saveFb, *m_dict,
        *m_reportToggle;
};
