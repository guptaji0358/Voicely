// Integration tests: C++ ProtocolClient <-> real Python worker (mock backend; no model or key needed).
#include "Config.h"
#include "ProtocolClient.h"

#include <QElapsedTimer>
#include <QFile>
#include <QSignalSpy>
#include <QTemporaryDir>
#include <QtTest>

Q_DECLARE_METATYPE(QJsonObject)

namespace {
struct Result { QString status, code, message; QJsonObject obj; };
}

class TstProtocol : public QObject {
    Q_OBJECT
    ProtocolClient *c = nullptr;
    QTemporaryDir tmp;

    Result wait(const QString &id, int ms = 30000) {
        Result r;
        bool done = false;
        auto conn = connect(c, &ProtocolClient::finished, this, [&](const QString &rid, const QString &st, const QJsonObject &o) {
            if (rid != id) return;
            r = {st, o.value("code").toString(), o.value("message").toString(), o};
            done = true;
        });
        QElapsedTimer t; t.start();
        while (!done && t.elapsed() < ms) QTest::qWait(10);
        disconnect(conn);
        if (!done) r.status = "timeout";
        return r;
    }
    static QString longText(int n) {
        QString s;
        for (int i = 0; i < n; ++i) s += QString("Sentence number %1 is here. ").arg(i);
        return s;
    }

private slots:
    void initTestCase() {
        qRegisterMetaType<QJsonObject>();
        QVERIFY(tmp.isValid());
        c = new ProtocolClient(this);
        QSignalSpy ready(c, &ProtocolClient::ready);
        c->start(Config::findPython(), Config::findEngineRoot());
        QVERIFY2(ready.wait(30000), "worker did not become ready");
    }
    void cleanupTestCase() { c->stop(); QTest::qWait(500); }

    void health() {
        auto r = wait(c->request("health"));
        QCOMPARE(r.status, QString("success"));
        QVERIFY(r.obj["backends"].toObject()["mock"].toObject()["available"].toBool());
        QVERIFY(r.obj["mp3"].toObject()["available"].toBool());
    }
    void unavailableBackend() {   // google without credentials -> structured error, no crash
        qunsetenv("GEMINI_API_KEY"); qunsetenv("GOOGLE_API_KEY");
        auto r = wait(c->request("synthesize", {{"text", "Hi"}, {"backend", "google"},
                                                {"output_path", tmp.filePath("g.wav")}}));
        QCOMPARE(r.status, QString("error"));
        QCOMPARE(r.code, QString("BACKEND_UNAVAILABLE"));
    }
    void unicodeSpacePathAndProgress() {
        QDir().mkpath(tmp.filePath("dir with spaces/音声 ü"));
        const QString out = tmp.filePath("dir with spaces/音声 ü/out put.wav");
        QList<int> prog;
        auto conn = connect(c, &ProtocolClient::progress, this, [&](const QString &, int p, const QString &) { prog << p; });
        auto r = wait(c->request("synthesize", {{"text", longText(15)}, {"backend", "mock"}, {"output_path", out}}));
        disconnect(conn);
        QCOMPARE(r.status, QString("success"));
        QVERIFY(QFileInfo(out).size() > 1000);
        QVERIFY(prog.size() > 5);
        QCOMPARE(prog.last(), 100);
        QVERIFY(std::is_sorted(prog.begin(), prog.end()));
    }
    void mp3ExportAndOverwriteRules() {
        const QString wav = tmp.filePath("src.wav"), mp3 = tmp.filePath("oé.mp3");
        QCOMPARE(wait(c->request("synthesize", {{"text", "Hello there."}, {"backend", "mock"}, {"output_path", wav}, {"overwrite", true}})).status, QString("success"));
        auto r = wait(c->request("export", {{"input_path", wav}, {"output_path", mp3}, {"bitrate_kbps", 192}}));
        QCOMPARE(r.status, QString("success"));
        QFile f(mp3); QVERIFY(f.open(QIODevice::ReadOnly));
        const QByteArray head = f.read(3);
        QVERIFY(head == "ID3" || (uchar(head[0]) == 0xFF && (uchar(head[1]) & 0xE0) == 0xE0));   // real MP3, not RIFF
        r = wait(c->request("export", {{"input_path", wav}, {"output_path", mp3}}));
        QCOMPARE(r.code, QString("INVALID_INPUT"));   // refuses to overwrite silently
    }
    void repeatedRequests() {
        for (int i = 0; i < 5; ++i) {
            auto r = wait(c->request("synthesize", {{"text", QString("Request %1.").arg(i)}, {"backend", "mock"},
                                                    {"output_path", tmp.filePath(QString("rep%1.wav").arg(i))}}));
            QCOMPARE(r.status, QString("success"));
        }
    }
    void errorsAreStructured() {
        QCOMPARE(wait(c->request("bogus")).code, QString("UNKNOWN_ACTION"));
        QCOMPARE(wait(c->request("synthesize", {{"text", ""}, {"backend", "mock"}, {"output_path", tmp.filePath("e.wav")}})).code, QString("INVALID_INPUT"));
        auto r = wait(c->request("synthesize", {{"text", "x [[fail]]"}, {"backend", "mock"}, {"output_path", tmp.filePath("e.wav")}}));
        QCOMPARE(r.code, QString("SYNTHESIS_FAILED"));
        QVERIFY(!QFileInfo::exists(tmp.filePath("e.wav")));
    }
    void missingPython() {
        ProtocolClient bad;
        QSignalSpy stopped(&bad, &ProtocolClient::workerStopped);
        bad.start(tmp.filePath("no such python.exe"), Config::findEngineRoot());
        QTRY_VERIFY_WITH_TIMEOUT(stopped.count() > 0, 10000);   // may be emitted synchronously
        QVERIFY(stopped.first().first().toString().contains("Could not start Python"));
        QSignalSpy fin(&bad, &ProtocolClient::finished);
        bad.request("health");
        QVERIFY(fin.wait(2000));
        QCOMPARE(fin.first().at(2).toJsonObject()["code"].toString(), QString("WORKER_NOT_RUNNING"));
    }
    void missingDependencyIsReported() {   // a Python that cannot import the engine -> worker exits, requests fail
        QTemporaryDir empty;
        ProtocolClient bad;
        QSignalSpy stopped(&bad, &ProtocolClient::workerStopped);
        bad.start(Config::findPython(), empty.path());   // no speech_engine here
        QVERIFY(stopped.wait(15000));
    }
    void cancelAndBusy() {
        QTemporaryDir d;
        ProtocolClient w;
        QSignalSpy ready(&w, &ProtocolClient::ready);
        QProcessEnvironment e; e.insert("SPEECH_ENGINE_MOCK_DELAY", "0.3");
        w.start(Config::findPython(), Config::findEngineRoot(), e);
        QVERIFY(ready.wait(30000));
        QString a = w.request("synthesize", {{"text", longText(30)}, {"backend", "mock"}, {"output_path", d.filePath("a.wav")}});
        QString b = w.request("synthesize", {{"text", "x"}, {"backend", "mock"}, {"output_path", d.filePath("b.wav")}});
        QSignalSpy fin(&w, &ProtocolClient::finished);
        QTRY_VERIFY_WITH_TIMEOUT(fin.count() >= 1, 5000);
        QCOMPARE(fin.at(0).at(0).toString(), b);
        QCOMPARE(fin.at(0).at(2).toJsonObject()["code"].toString(), QString("BUSY"));
        QElapsedTimer t; t.start();
        w.cancel(a);
        QTRY_VERIFY_WITH_TIMEOUT([&] { for (auto &x : fin) if (x.at(0).toString() == a) return true; return false; }(), 5000);
        QVERIFY(t.elapsed() < 3000);
        for (auto &x : fin) if (x.at(0).toString() == a) QCOMPARE(x.at(1).toString(), QString("cancelled"));
        QVERIFY(!QFileInfo::exists(d.filePath("a.wav")));
        // Application shutdown while a job is running: worker exits, no orphan
        QString c2 = w.request("synthesize", {{"text", longText(30)}, {"backend", "mock"}, {"output_path", d.filePath("c.wav")}});
        QSignalSpy stopped(&w, &ProtocolClient::workerStopped);
        w.stop();
        QVERIFY(stopped.wait(8000));
    }
    void workerCrashFailsPending() {
        ProtocolClient w;
        QSignalSpy ready(&w, &ProtocolClient::ready);
        QProcessEnvironment e; e.insert("SPEECH_ENGINE_MOCK_DELAY", "5");
        w.start(Config::findPython(), Config::findEngineRoot(), e);
        QVERIFY(ready.wait(30000));
        QString id = w.request("synthesize", {{"text", "hi"}, {"backend", "mock"}, {"output_path", tmp.filePath("crash.wav")}});
        QSignalSpy fin(&w, &ProtocolClient::finished);
        w.stop();   // closing stdin cancels the job and the worker exits; the pending request must resolve
        QVERIFY(fin.wait(8000));
        QCOMPARE(fin.first().at(0).toString(), id);
    }
};

int main(int argc, char **argv) {
    QCoreApplication app(argc, argv);
    QSettings::setDefaultFormat(QSettings::IniFormat);
    QTemporaryDir cfg;
    QSettings::setPath(QSettings::IniFormat, QSettings::UserScope, cfg.path());
    TstProtocol t;
    return QTest::qExec(&t, argc, argv);
}
#include "tst_protocol.moc"
