#pragma once
// Asynchronous client for the speech_engine.worker JSON-lines protocol (v1, see docs/PROTOCOL.md).
// Never blocks the GUI thread during normal operation; everything is signal driven.

#include <QHash>
#include <QJsonObject>
#include <QObject>
#include <QProcess>
#include <QProcessEnvironment>
#include <QTimer>

class ProtocolClient : public QObject {
    Q_OBJECT
public:
    static constexpr int kProtocolVersion = 1;

    explicit ProtocolClient(QObject *parent = nullptr);
    ~ProtocolClient() override;

    // Launches `<python> -X utf8 -m speech_engine.worker` with cwd = engineRoot.
    void start(const QString &pythonExe, const QString &engineRoot,
               const QProcessEnvironment &extraEnv = {});
    void stop();                       // graceful: closes stdin, kills after a short grace period
    bool isRunning() const { return m_proc.state() == QProcess::Running; }
    bool isReady() const { return m_ready; }

    // Returns the request id. The reply arrives via finished(); progress via progress().
    // timeoutMs <= 0 means no timeout.
    QString request(const QString &action, QJsonObject params = {}, int timeoutMs = 0);
    void cancel(const QString &requestId);
    int pendingCount() const { return m_pending.size(); }

signals:
    void ready();                                               // worker sent its "ready" event
    void progress(const QString &id, int percent, const QString &message);
    // status: "success" | "error" | "cancelled". For error: code + message in `response`.
    void finished(const QString &id, const QString &status, const QJsonObject &response);
    void workerStopped(const QString &reason);                  // process failed to start or ended
    void logLine(const QString &line);                          // worker stderr + protocol warnings

private slots:
    void onStdout();
    void onStderr();
    void onError(QProcess::ProcessError e);
    void onFinished(int code, QProcess::ExitStatus st);

private:
    void handleLine(const QByteArray &line);
    void failAll(const QString &code, const QString &message);
    void complete(const QString &id, const QString &status, const QJsonObject &obj);

    QProcess m_proc;
    QByteArray m_outBuf, m_errBuf;
    QHash<QString, QTimer *> m_pending;   // id -> timeout timer (may be null-timer for no timeout)
    bool m_ready = false;
    bool m_stopping = false;
    QString m_python;
};
