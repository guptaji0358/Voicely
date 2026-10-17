#pragma once
// "Pronunciation" settings: speech-recognition model, optional AI assistance, feedback consent / inspect / export /
// delete, and the dictionary. Changes apply immediately through the worker (nothing here stores credentials).
#include <QJsonObject>
#include <QWidget>

class ProtocolClient;
class QCheckBox;
class QComboBox;
class QLabel;
class QLineEdit;
class QPushButton;

class PronSettings : public QWidget {
    Q_OBJECT
public:
    PronSettings(ProtocolClient *client, const QString &backend, bool ipaSupported, QWidget *parent = nullptr);
    void refresh();

signals:
    void changed();   // ASR / LLM availability may have changed

private:
    ProtocolClient *m_client;
    QString m_backend;
    bool m_ipa;
    QLabel *m_asrStatus, *m_llmStatus, *m_fbStatus;
    QPushButton *m_download;
    QCheckBox *m_llmOn, *m_examples, *m_fbOn, *m_fbAudio;
    QComboBox *m_provider;
    QLineEdit *m_model;
};
