#include "ProtocolClient.h"

#include <QJsonDocument>
#include <QUuid>

ProtocolClient::ProtocolClient(QObject *parent) : QObject(parent) {
    m_proc.setProcessChannelMode(QProcess::SeparateChannels);
    connect(&m_proc, &QProcess::readyReadStandardOutput, this, &ProtocolClient::onStdout);
    connect(&m_proc, &QProcess::readyReadStandardError, this, &ProtocolClient::onStderr);
    connect(&m_proc, &QProcess::errorOccurred, this, &ProtocolClient::onError);
    connect(&m_proc, &QProcess::finished, this, &ProtocolClient::onFinished);
}

ProtocolClient::~ProtocolClient() {
    m_stopping = true;
    blockSignals(true);   // owners may already be partly destroyed; never call back into them
    m_proc.disconnect(this);
    if (m_proc.state() != QProcess::NotRunning) {
        m_proc.closeWriteChannel();
        if (!m_proc.waitForFinished(2000)) {   // shutdown path only; never during normal operation
            m_proc.kill();
            m_proc.waitForFinished(2000);
        }
    }
}

void ProtocolClient::start(const QString &pythonExe, const QString &engineRoot,
                           const QProcessEnvironment &extraEnv) {
    if (m_proc.state() != QProcess::NotRunning) return;
    m_ready = false;
    m_stopping = false;
    m_outBuf.clear();
    m_errBuf.clear();
    m_python = pythonExe;
    QProcessEnvironment env = QProcessEnvironment::systemEnvironment();
    env.insert(extraEnv);
    env.insert("PYTHONIOENCODING", "utf-8");
    env.insert("PYTHONUNBUFFERED", "1");
    env.insert("PYTHONPATH", engineRoot);        // works regardless of cwd
    m_proc.setProcessEnvironment(env);
    m_proc.setWorkingDirectory(engineRoot);
    // Program + argument list: no shell, no manual quoting; spaces / Unicode paths are safe.
    m_proc.setProgram(pythonExe);
    m_proc.setArguments({"-X", "utf8", "-m", "speech_engine.worker"});
    m_proc.start();
}

void ProtocolClient::stop() {
    m_stopping = true;
    if (m_proc.state() == QProcess::NotRunning) return;
    m_proc.closeWriteChannel();                  // worker cancels its job and exits on EOF
    QTimer::singleShot(3000, this, [this] {
        if (m_proc.state() != QProcess::NotRunning) m_proc.kill();
    });
}

QString ProtocolClient::request(const QString &action, QJsonObject params, int timeoutMs) {
    const QString id = QUuid::createUuid().toString(QUuid::WithoutBraces);
    if (m_proc.state() != QProcess::Running) {
        QTimer::singleShot(0, this, [this, id] {
            emit finished(id, "error", {{"request_id", id}, {"status", "error"}, {"code", "WORKER_NOT_RUNNING"},
                                        {"message", "The Python worker is not running."}});
        });
        return id;
    }
    params["protocol_version"] = kProtocolVersion;
    params["request_id"] = id;
    params["action"] = action;
    QTimer *t = nullptr;
    if (timeoutMs > 0) {
        t = new QTimer(this);
        t->setSingleShot(true);
        connect(t, &QTimer::timeout, this, [this, id] {
            complete(id, "error", {{"request_id", id}, {"status", "error"}, {"code", "TIMEOUT"},
                                   {"message", "The worker did not answer in time."}});
        });
        t->start(timeoutMs);
    }
    m_pending.insert(id, t);
    m_proc.write(QJsonDocument(params).toJson(QJsonDocument::Compact) + '\n');
    return id;
}

void ProtocolClient::cancel(const QString &requestId) {
    if (m_proc.state() != QProcess::Running) return;
    request("cancel", {{"target", requestId}});
}

void ProtocolClient::onStdout() {
    m_outBuf += m_proc.readAllStandardOutput();
    int nl;
    while ((nl = m_outBuf.indexOf('\n')) >= 0) {       // handles partial reads and multiple lines per read
        const QByteArray line = m_outBuf.left(nl).trimmed();
        m_outBuf.remove(0, nl + 1);
        if (!line.isEmpty()) handleLine(line);
    }
}

void ProtocolClient::onStderr() {
    m_errBuf += m_proc.readAllStandardError();
    int nl;
    while ((nl = m_errBuf.indexOf('\n')) >= 0) {
        const QString line = QString::fromUtf8(m_errBuf.left(nl)).trimmed();
        m_errBuf.remove(0, nl + 1);
        if (!line.isEmpty()) emit logLine(line);
    }
}

void ProtocolClient::handleLine(const QByteArray &line) {
    QJsonParseError pe;
    const QJsonDocument doc = QJsonDocument::fromJson(line, &pe);
    if (pe.error != QJsonParseError::NoError || !doc.isObject()) {
        emit logLine("[protocol] ignored malformed message: " + QString::fromUtf8(line.left(200)));
        return;
    }
    const QJsonObject o = doc.object();
    if (o.value("protocol_version").toInt() != kProtocolVersion) {
        emit logLine("[protocol] ignored message with unsupported protocol_version");
        return;
    }
    const QString id = o.value("request_id").toString();
    const QString event = o.value("event").toString();
    if (event == "ready") {
        m_ready = true;
        emit ready();
    } else if (event == "progress") {
        if (m_pending.contains(id)) emit progress(id, o.value("progress").toInt(), o.value("message").toString());
    } else if (o.contains("status")) {
        const QString st = o.value("status").toString();
        if (m_pending.contains(id)) complete(id, st, o);
        else if (!id.isEmpty()) emit logLine("[protocol] reply for unknown request " + id);
        else emit logLine("[protocol] worker error: " + o.value("message").toString());
    }
}

void ProtocolClient::complete(const QString &id, const QString &status, const QJsonObject &obj) {
    QTimer *t = m_pending.take(id);
    if (t) { t->stop(); t->deleteLater(); }
    emit finished(id, status, obj);
}

void ProtocolClient::failAll(const QString &code, const QString &message) {
    const auto ids = m_pending.keys();
    for (const QString &id : ids)
        complete(id, "error", {{"request_id", id}, {"status", "error"}, {"code", code}, {"message", message}});
}

void ProtocolClient::onError(QProcess::ProcessError e) {
    if (e == QProcess::FailedToStart) {
        const QString msg = QString("Could not start Python (%1): %2. Set a valid interpreter in Settings.")
                                .arg(m_python, m_proc.errorString());
        failAll("WORKER_START_FAILED", msg);
        emit workerStopped(msg);
    }
    // Crashed etc. are reported through finished().
}

void ProtocolClient::onFinished(int code, QProcess::ExitStatus st) {
    onStdout();
    onStderr();
    m_ready = false;
    const QString msg = m_stopping ? "Worker stopped."
                        : st == QProcess::CrashExit ? "The Python worker crashed."
                                                     : QString("The Python worker exited unexpectedly (code %1).").arg(code);
    failAll("WORKER_EXITED", msg);
    emit workerStopped(msg);
}
