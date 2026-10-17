#pragma once
// Asynchronous client for the speech_engine.worker JSON-lines protocol (v1, see docs/PROTOCOL.md).
// Never blocks the GUI thread during normal operation; everything is signal driven.

#include <QHash>
#include <QPointer>
#include <functional>
#include <QJsonObject>
#include <QObject>
#include <QProcess>
#include <QProcessEnvironment>
#include <QTimer>

class ProtocolClient : public QObject {
    Q_OBJECT
public:
    static constexpr int kProtocolVersion = 2;   // the worker also answers version-1 requests (see docs/PROTOCOL.md)

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
    // Same as request(), but the result is delivered to `cb` (only while `ctx` is alive) in addition to finished().
    using Callback = std::function<void(const QString &status, const QJsonObject &response)>;
    QString call(const QString &action, QJsonObject params, QObject *ctx, Callback cb, int timeoutMs = 60000);
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
    struct Cb { QPointer<QObject> ctx; Callback fn; };
    QHash<QString, Cb> m_callbacks;
    QHash<QString, QTimer *> m_pending;   // id -> timeout timer (may be null-timer for no timeout)
    bool m_ready = false;
    bool m_stopping = false;
    QString m_python;
};
