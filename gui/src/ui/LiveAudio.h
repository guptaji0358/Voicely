#pragma once
// LiveAudio - plays 16-bit mono PCM that is still being produced ("play while generating").
//
// The worker finishes a long text chunk by chunk. Each finished chunk is appended here and goes straight to the speakers
// through one push-mode QAudioSink, so playback starts after the first sentences instead of after the whole document and
// the joins between chunks are sample-exact (no gap, no click). If generation falls behind, the sink simply idles until
// the next chunk arrives (nothing is repeated and no silence is invented). The final, complete file is still written by
// the worker as before; this class only previews it early and never touches files. The owner stops it when the
// finished file takes over (playedMs() says where to continue).
#include <QAudioFormat>
#include <QByteArray>
#include <QObject>
#include <QTimer>
#include <memory>

class QAudioSink;
class QIODevice;

class LiveAudio : public QObject {
    Q_OBJECT
public:
    explicit LiveAudio(QObject *parent = nullptr);
    ~LiveAudio() override;

    static bool available();                       // an audio output device exists

    bool begin(int sampleRate);                    // false: no device / unsupported format (nothing started)
    void append(const QByteArray &pcm);            // 16-bit little-endian mono samples
    void appendSilenceMs(int ms);
    void pause();
    void resume();
    void stop();                                   // abandon everything that has not been played

    bool active() const { return m_active; }       // begun and not yet stopped
    bool paused() const { return m_paused; }
    qint64 playedMs() const;                       // how much has actually been played
    qint64 queuedMs() const;                       // how much has been appended in total
    int sampleRate() const { return m_rate; }

signals:
    void playbackStarted();                        // the first sound is going out
    void stateChanged();                           // active / paused changed

private:
    void pump();
    void startSink();
    void teardown();

    std::unique_ptr<QAudioSink> m_sink;
    QIODevice *m_io = nullptr;
    QByteArray m_pending;                          // appended but not yet accepted by the sink
    QTimer m_timer;
    int m_rate = 0;
    qint64 m_totalBytes = 0;
    bool m_active = false, m_paused = false, m_sinkStarted = false;
};
