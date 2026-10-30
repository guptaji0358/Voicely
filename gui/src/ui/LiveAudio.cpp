#include "LiveAudio.h"

#include <QAudioDevice>
#include <QAudioSink>
#include <QMediaDevices>
#include <algorithm>

namespace {
constexpr int kPumpMs = 25;
constexpr double kPrerollSeconds = 0.35;   // buffered before the sink starts, so a slow first write cannot stutter
}  // namespace

LiveAudio::LiveAudio(QObject *parent) : QObject(parent) {
    m_timer.setInterval(kPumpMs);
    connect(&m_timer, &QTimer::timeout, this, &LiveAudio::pump);
}

LiveAudio::~LiveAudio() { teardown(); }

bool LiveAudio::available() { return !QMediaDevices::defaultAudioOutput().isNull(); }

bool LiveAudio::begin(int sampleRate) {
    teardown();
    const QAudioDevice dev = QMediaDevices::defaultAudioOutput();
    if (dev.isNull() || sampleRate < 8000 || sampleRate > 192000) return false;
    QAudioFormat fmt;
    fmt.setSampleRate(sampleRate);
    fmt.setChannelCount(1);
    fmt.setSampleFormat(QAudioFormat::Int16);
    if (!dev.isFormatSupported(fmt)) return false;
    m_sink = std::make_unique<QAudioSink>(dev, fmt);
    m_sink->setBufferSize(sampleRate * 2);              // one second of audio in the device queue
    m_rate = sampleRate;
    m_active = true;
    m_paused = m_sinkStarted = false;
    m_totalBytes = 0;
    m_pending.clear();
    m_timer.start();
    emit stateChanged();
    return true;
}

void LiveAudio::append(const QByteArray &pcm) {
    if (!m_active || pcm.isEmpty()) return;
    m_pending += pcm;
    m_totalBytes += pcm.size();
    pump();
}

void LiveAudio::appendSilenceMs(int ms) {
    if (!m_active || ms <= 0) return;
    append(QByteArray(int(qint64(m_rate) * ms / 1000) * 2, '\0'));
}

void LiveAudio::startSink() {
    if (m_sinkStarted || !m_sink) return;
    m_io = m_sink->start();            // push mode: we write into the returned device
    m_sinkStarted = true;
    if (m_paused) m_sink->suspend();
    emit playbackStarted();
}

void LiveAudio::pump() {
    if (!m_active) return;
    if (!m_sinkStarted) {
        if (m_pending.size() < int(m_rate * 2 * kPrerollSeconds)) return;   // wait for a little audio first
        startSink();
    }
    if (!m_io || m_paused || !m_sink) return;
    while (!m_pending.isEmpty()) {
        const qint64 room = m_sink->bytesFree();
        if (room < 2) break;
        const qint64 n = std::min<qint64>(room & ~1LL, m_pending.size());
        const qint64 w = m_io->write(m_pending.constData(), n);
        if (w <= 0) break;
        m_pending.remove(0, int(w));
    }
}

void LiveAudio::pause() {
    if (!m_active || m_paused) return;
    m_paused = true;
    if (m_sinkStarted && m_sink) m_sink->suspend();
    emit stateChanged();
}

void LiveAudio::resume() {
    if (!m_active || !m_paused) return;
    m_paused = false;
    if (m_sinkStarted && m_sink) m_sink->resume();
    emit stateChanged();
    pump();
}

void LiveAudio::stop() {
    const bool was = m_active;
    teardown();
    if (was) emit stateChanged();
}

void LiveAudio::teardown() {
    m_timer.stop();
    if (m_sink) {
        m_sink->stop();
        m_sink.reset();
    }
    m_io = nullptr;
    m_pending.clear();
    m_active = m_paused = m_sinkStarted = false;
}

qint64 LiveAudio::playedMs() const {
    return (m_sink && m_sinkStarted && m_rate) ? m_sink->processedUSecs() / 1000 : 0;
}

qint64 LiveAudio::queuedMs() const { return m_rate ? (m_totalBytes / 2) * 1000 / m_rate : 0; }
