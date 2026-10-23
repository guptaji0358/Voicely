#pragma once
// Settings window: a left navigation list (General / Pronunciation) beside a stack of pages. General is split into
// titled cards (Engine, Google API key, Appearance, Storage); everything is styled by the theme's QSS
// (objectNames settingsNav, settingsHeader, settingsCard, ...). The dialog only collects values: the caller reads
// them after exec() == Accepted and applies them.
#include <QDialog>

class PronSettings;
class ProtocolClient;
class QCheckBox;
class QComboBox;
class QLineEdit;
class QListWidget;
class QSpinBox;
class QStackedWidget;

class SettingsDialog : public QDialog {
    Q_OBJECT
public:
    SettingsDialog(ProtocolClient *client, const QString &backend, bool ipaSupported, const QString &apiKey,
                   QWidget *parent = nullptr);

    QString pythonExe() const;
    QString engineRoot() const;
    QString apiKey() const;
    QString theme() const;
    bool reduceMotion() const;
    bool effects() const;
    bool sounds() const;
    int tempKeepDays() const;
    bool nativeFrame() const;

signals:
    void pronunciationChanged();

private:
    QWidget *buildGeneral(const QString &apiKey);
    QListWidget *m_nav;
    QStackedWidget *m_pages;
    QLineEdit *m_py, *m_root, *m_key;
    QComboBox *m_theme;
    QCheckBox *m_reduce, *m_fx, *m_snd, *m_native;
    QSpinBox *m_keep;
    PronSettings *m_pron;
};
