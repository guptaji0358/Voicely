#pragma once
// Short UI sound effects (compiled-in WAVs made by packaging/make_ui_sounds.py). One QSoundEffect per sound, kept on the
// current default output device. Off with the "uiSounds" setting (Settings dialog), TTS_NO_SOUND=1, or the offscreen
// platform (tests). Also installs an application-wide filter that clicks on enabled buttons.
#include <QHash>
#include <QObject>
#include <QSoundEffect>

class UiSounds : public QObject {
    Q_OBJECT
public:
    enum class Sound { Click, Toggle, Generate, Success, Error, DragOver, Drop };

    static UiSounds &instance();
    bool enabled() const;
    void setEnabled(bool on);
    void play(Sound s);
    void installClickFilter();             // clicks on enabled buttons (once, idempotent)

protected:
    bool eventFilter(QObject *, QEvent *) override;

private:
    UiSounds() = default;
    QSoundEffect *effect(Sound s);
    QHash<int, QSoundEffect *> m_fx;
    bool m_filter = false;
};
