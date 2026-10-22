#include "UiSounds.h"

#include "Config.h"

#include <QAbstractButton>
#include <QApplication>
#include <QAudioDevice>
#include <QEvent>
#include <QMediaDevices>
#include <QMouseEvent>
#include <QUrl>

UiSounds &UiSounds::instance() {
    static UiSounds s;
    return s;
}

bool UiSounds::enabled() const {
    if (!qEnvironmentVariableIsEmpty("TTS_NO_SOUND") || QGuiApplication::platformName() == "offscreen") return false;
    return Config::settings().value("uiSounds", true).toBool();
}

void UiSounds::setEnabled(bool on) { Config::settings().setValue("uiSounds", on); }

QSoundEffect *UiSounds::effect(Sound s) {
    static const char *files[] = {"click", "toggle", "generate", "success", "error", "dragover", "drop"};
    static const float volume[] = {0.35f, 0.40f, 0.50f, 0.55f, 0.55f, 0.35f, 0.55f};
    const int k = int(s);
    QSoundEffect *&e = m_fx[k];
    if (!e) {
        e = new QSoundEffect(this);
        e->setSource(QUrl(QString("qrc:/sounds/%1.wav").arg(files[k])));
        e->setVolume(volume[k]);
    }
    const QAudioDevice dev = QMediaDevices::defaultAudioOutput();     // follow the speakers/earphones the user is using
    if (!dev.isNull() && e->audioDevice() != dev) e->setAudioDevice(dev);
    return e;
}

void UiSounds::play(Sound s) {
    if (!enabled()) return;
    effect(s)->play();
}

void UiSounds::installClickFilter() {
    if (m_filter) return;
    m_filter = true;
    qApp->installEventFilter(this);
}

bool UiSounds::eventFilter(QObject *o, QEvent *e) {
    if (e->type() == QEvent::MouseButtonPress && static_cast<QMouseEvent *>(e)->button() == Qt::LeftButton) {
        if (auto *b = qobject_cast<QAbstractButton *>(o)) {
            // No click under the Play button: a second stream opening at the same instant makes playback start with a glitch.
            if (b->isEnabled() && b->objectName() != "primary" && b->objectName() != "playButton") play(b->isCheckable() ? Sound::Toggle : Sound::Click);
        }
    }
    return false;
}
