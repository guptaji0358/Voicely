#include "ModelLabDialog.h"
#include "Controls.h"

#include "Buttons.h"
#include "Config.h"
#include "Icons.h"
#include "Indicators.h"
#include "ProtocolClient.h"
#include "ThemeManager.h"

#include <QCheckBox>
#include <QComboBox>
#include <QDoubleSpinBox>
#include <QFileDialog>
#include <QFormLayout>
#include <QGridLayout>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QLabel>
#include <QLineEdit>
#include <QMessageBox>
#include <QPainter>
#include <QPlainTextEdit>
#include <QSpinBox>
#include <QTreeWidget>
#include <QVBoxLayout>
#include <QtMath>

namespace {
QString mb(double bytes) { return QString("%1 MB").arg(bytes / (1024.0 * 1024.0), 0, 'f', bytes > 1e9 ? 0 : 1); }
QString params(qint64 n) {
    if (n >= 1000000000) return QString::number(n / 1e9, 'f', 2) + " B";
    if (n >= 1000000) return QString::number(n / 1e6, 'f', 2) + " M";
    if (n >= 1000) return QString::number(n / 1e3, 'f', 1) + " K";
    return QString::number(n);
}
QString pct(double v) { return QString::number(v * 100, 'f', 1) + "%"; }
}  // namespace

// ------------------------------------------------------------------------------------------ LossChart
LossChart::LossChart(QWidget *parent) : QWidget(parent) {
    setObjectName("lossChart");
    setMinimumHeight(120);
}

void LossChart::clear() { m_train.clear(); m_val.clear(); update(); }
void LossChart::addTrain(int step, double loss) { if (qIsFinite(loss)) { m_train.append({double(step), loss}); update(); } }
void LossChart::addVal(int step, double loss) { if (qIsFinite(loss)) { m_val.append({double(step), loss}); update(); } }

void LossChart::paintEvent(QPaintEvent *) {
    QPainter p(this);
    p.setRenderHint(QPainter::Antialiasing);
    const auto &t = ThemeManager::instance();
    const QColor grid = t.color("border"), muted = t.color("text-muted"), train = t.color("accent"), val = t.color("success");
    const QRectF plot = QRectF(rect()).adjusted(46, 14, -12, -26);
    p.setPen(grid);
    p.drawRect(plot);
    if (m_train.isEmpty() && m_val.isEmpty()) {
        p.setPen(muted);
        p.drawText(rect(), Qt::AlignCenter, tr("No training data yet.\nLoss appears here as a real run reports it."));
        return;
    }
    double x1 = 1, lo = 1e300, hi = -1e300;
    for (const auto *s : {&m_train, &m_val})
        for (const QPointF &pt : *s) { x1 = qMax(x1, pt.x()); lo = qMin(lo, pt.y()); hi = qMax(hi, pt.y()); }
    if (hi - lo < 1e-9) { hi = lo + 1; }
    const double pad = (hi - lo) * 0.08;
    lo = qMax(0.0, lo - pad); hi += pad;
    auto map = [&](const QPointF &pt) {
        return QPointF(plot.left() + plot.width() * pt.x() / x1, plot.bottom() - plot.height() * (pt.y() - lo) / (hi - lo));
    };
    QFont f = font(); f.setPointSizeF(f.pointSizeF() * 0.85); p.setFont(f);
    p.setPen(muted);
    for (int i = 0; i <= 4; ++i) {                              // y grid + labels
        const double v = lo + (hi - lo) * i / 4, y = plot.bottom() - plot.height() * i / 4;
        p.setPen(QPen(grid, 1, Qt::DotLine));
        p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y));
        p.setPen(muted);
        p.drawText(QRectF(0, y - 8, plot.left() - 4, 16), Qt::AlignRight | Qt::AlignVCenter, QString::number(v, 'f', v < 10 ? 3 : 1));
    }
    p.drawText(QRectF(plot.left(), plot.bottom() + 4, plot.width(), 16), Qt::AlignRight, tr("step %1").arg(int(x1)));
    auto line = [&](const QVector<QPointF> &s, const QColor &c, bool dots) {
        if (s.isEmpty()) return;
        QPolygonF poly;
        for (const QPointF &pt : s) poly << map(pt);
        p.setPen(QPen(c, 2, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
        p.drawPolyline(poly);
        if (dots) { p.setBrush(c); for (const QPointF &q : poly) p.drawEllipse(q, 2.5, 2.5); p.setBrush(Qt::NoBrush); }
    };
    line(m_train, train, false);
    line(m_val, val, true);
    p.setPen(train); p.drawText(QPointF(plot.left() + 8, plot.top() + 12), tr("train loss"));
    p.setPen(val); p.drawText(QPointF(plot.left() + 90, plot.top() + 12), tr("validation loss"));
}

// ------------------------------------------------------------------------------------------ dialog
QString ModelLabDialog::stateText(const QString &s, bool training) {
    if (training) return tr("Training in progress");
    if (s == "random_init") return tr("Randomly initialized (untrained)");
    if (s == "expanded") return tr("Expanded - needs training before it is any better");
    if (s == "trained") return tr("Training completed (not yet evaluated)");
    if (s == "eval_passed") return tr("Evaluation passed");
    if (s == "eval_failed") return tr("Evaluation failed");
    if (s == "promoted") return tr("Promoted to production");
    return tr("No model yet");
}

ModelLabDialog::ModelLabDialog(const QProcessEnvironment &env, QWidget *parent) : StyledDialog(parent, "ai"), m_client(new ProtocolClient(this)) {
    setObjectName("modelLab");
    setWindowTitle(tr("Model Lab"));
    setWindowIcon(Icons::icon("ai"));
    m_confirm = [this](const QString &title, const QString &text) {
        return Msg::question(this, title, text) == QMessageBox::Yes;
    };
    resize(1100, 800);
    setMinimumSize(920, 640);
    buildUi();
    connect(m_client, &ProtocolClient::logLine, this, [this](const QString &l) { log(l); });
    connect(m_client, &ProtocolClient::progress, this, [this](const QString &id, int pct, const QString &msg) {
        if (id != m_jobId) return;
        if (m_phase == Phase::Training) m_progress->setTarget(pct);
        m_step->setText(msg);
    });
    connect(m_client, &ProtocolClient::eventReceived, this, [this](const QString &id, const QString &name, const QJsonObject &o) {
        if (id != m_jobId || name != "metric") return;
        const QJsonObject m = o.value("metric").toObject();
        if (m.value("type").toString() == "train") m_chart->addTrain(m.value("step").toInt(), m.value("loss").toDouble());
        else if (m.value("type").toString() == "val") m_chart->addVal(m.value("step").toInt(), m.value("loss").toDouble());
    });
    connect(m_client, &ProtocolClient::workerStopped, this, [this](const QString &reason) {
        m_jobId.clear();
        setPhase(Phase::Unavailable, reason);
    });
    connect(m_client, &ProtocolClient::ready, this, [this] {
        m_client->call("health", {}, this, [this](const QString &st, const QJsonObject &r) {
            if (st != "success") { showError(r.value("code").toString(), r.value("message").toString()); setPhase(Phase::Unavailable, r.value("message").toString()); return; }
            m_torch = r.value("torch_available").toBool();
            if (!m_torch) {
                m_banner->setText(r.value("problem").toString());
                m_banner->show();
                setPhase(Phase::Unavailable, tr("PyTorch is not available for Model Lab"));
                // the opt-in switches still work without PyTorch: they only touch local files
                m_client->call("experience", {{"op", "stats"}}, this, [this](const QString &s2, const QJsonObject &e) { if (s2 == "success") updateExperience(e); });
                return;
            }
            const auto mem = r.value("memory").toObject();
            m_hardware->setText(tr("PyTorch %1 - %2 - %3 CPU threads - RAM free %4 of %5")
                                    .arg(r.value("torch").toString(), r.value("cuda").toBool() ? r.value("cuda_device").toString() : tr("CPU only"))
                                    .arg(r.value("torch_threads").toInt())
                                    .arg(mb(mem.value("available").toDouble()), mb(mem.value("total").toDouble())));
            setPhase(Phase::Ready, tr("Model Lab worker ready"));
            refresh();
        }, 90000);
    });
    const QString root = Config::findEngineRoot();
    const QString py = Config::findLabPython();
    if (py.isEmpty() || root.isEmpty()) {
        setPhase(Phase::Unavailable, tr("Python or the engine folder was not found (see Settings)."));
    } else {
        log("lab python: " + py);
        m_client->start(py, root, env, "model_lab.worker");
    }
}

ModelLabDialog::~ModelLabDialog() {
    m_client->disconnect(this);
    m_client->stop();
}

void ModelLabDialog::buildUi() {
    auto *root = new QVBoxLayout(this);
    root->setContentsMargins(10, 4, 10, 8);
    root->setSpacing(10);

    auto *head = new QHBoxLayout;
    auto *title = new QLabel(tr("Model Lab")); title->setObjectName("title");
    m_icon = new StateIcon(18);
    m_status = new QLabel(tr("Starting Model Lab worker...")); m_status->setObjectName("statusLabel");
    m_status->setWordWrap(true);
    m_stateLabel = new QLabel(tr("No model yet")); m_stateLabel->setObjectName("labState");
    m_stateLabel->setStyleSheet("padding:3px 10px; border-radius:10px; background:rgba(255,255,255,0.10);");
    head->addWidget(title); head->addSpacing(10); head->addWidget(m_icon); head->addWidget(m_status, 1); head->addWidget(m_stateLabel);
    root->addLayout(head);
    m_hardware = new QLabel; m_hardware->setObjectName("muted");
    root->addWidget(m_hardware);
    m_banner = new QLabel; m_banner->setWordWrap(true); m_banner->setObjectName("labBanner"); m_banner->hide();
    m_banner->setStyleSheet("color:#e5b84a;");
    root->addWidget(m_banner);

    auto *cols = new QHBoxLayout; cols->setSpacing(12);
    // ---- left: the model, learning from my speech, dataset
    auto *left = new QVBoxLayout;
    m_tree = new QTreeWidget; m_tree->setObjectName("modelTree");
    m_tree->setHeaderLabels({tr("Model"), tr("Parameters"), tr("Layers/hidden/heads"), tr("State"), tr("Steps"), tr("Val acc.")});
    m_tree->setRootIsDecorated(false); m_tree->setAlternatingRowColors(true);
    m_tree->setMaximumHeight(110);
    m_tree->header()->setStretchLastSection(false);
    m_tree->header()->setSectionResizeMode(0, QHeaderView::Stretch);
    left->addWidget(new QLabel(tr("Your model (only one exists; a trained candidate appears here until you decide)")));
    left->addWidget(m_tree);

    auto *lg = new QGroupBox(tr("Learn from my speech"));
    auto *ll = new QVBoxLayout(lg);
    m_consent = new FancyCheckBox(tr("Let the model learn from the speech I generate (kept only on this computer)"));
    m_consent->setObjectName("labConsent");
    m_consent->setToolTip(tr("Off by default. When on, the TEXT of each generation is remembered; Recheck Speech labels it (issues found / verified). "
                             "Audio is not stored. Nothing is uploaded."));
    m_experienceLabel = new QLabel(tr("My speech: no data yet.")); m_experienceLabel->setObjectName("labExperience"); m_experienceLabel->setWordWrap(true);
    auto *lb = new QHBoxLayout;
    m_btnLearn = new AnimatedButton(tr("Train from my speech")); m_btnLearn->setObjectName("labLearn");
    m_btnExport = new AnimatedButton(tr("Export...")); m_btnExport->setObjectName("labExport");
    m_btnForget = new AnimatedButton(tr("Delete my stored speech data")); m_btnForget->setObjectName("labForget");
    lb->addWidget(m_btnLearn, 1); lb->addWidget(m_btnExport); lb->addWidget(m_btnForget);
    ll->addWidget(m_consent); ll->addWidget(m_experienceLabel); ll->addLayout(lb);
    left->addWidget(lg);

    auto *dsRow = new QHBoxLayout;
    m_dataset = new QComboBox; m_dataset->setObjectName("datasetCombo"); m_dataset->setMinimumWidth(240);
    m_btnData = new AnimatedButton(tr("New synthetic dataset")); m_btnData->setObjectName("labMakeData");
    dsRow->addWidget(new QLabel(tr("Dataset"))); dsRow->addWidget(m_dataset, 1); dsRow->addWidget(m_btnData);
    left->addLayout(dsRow);
    auto *mk = new QHBoxLayout;
    m_name = new QLineEdit("main"); m_name->setObjectName("labName");
    m_size = new QComboBox; m_size->setObjectName("labSize");
    for (const char *s : {"1K", "10K", "100K", "1M", "10M", "100M", "1B", "8B"}) m_size->addItem(s);
    m_size->setCurrentText("100K");
    m_size->setToolTip(tr("Architecture target. Sizes that do not fit in free memory are refused, not faked."));
    m_btnCreate = new AnimatedButton(tr("Create random model")); m_btnCreate->setObjectName("labCreate");
    mk->addWidget(new QLabel(tr("Name"))); mk->addWidget(m_name, 1); mk->addWidget(m_size); mk->addWidget(m_btnCreate);
    left->addLayout(mk);
    left->addStretch(1);
    cols->addLayout(left, 5);

    // ---- right: training
    auto *right = new QVBoxLayout;
    auto *tg = new QGroupBox(tr("Training (makes a candidate; nothing is replaced until you accept it)"));
    auto *tf = new QFormLayout(tg);
    m_steps = new QSpinBox; m_steps->setRange(1, 1000000); m_steps->setValue(600); m_steps->setObjectName("labSteps");
    m_batch = new QSpinBox; m_batch->setRange(1, 1024); m_batch->setValue(32); m_batch->setObjectName("labBatch");
    m_lr = new QDoubleSpinBox; m_lr->setDecimals(5); m_lr->setRange(0.00001, 1.0); m_lr->setSingleStep(0.0005); m_lr->setValue(0.003); m_lr->setObjectName("labLr");
    tf->addRow(tr("Steps"), m_steps); tf->addRow(tr("Batch size"), m_batch); tf->addRow(tr("Learning rate"), m_lr);
    auto *tb = new QHBoxLayout;
    m_btnTrain = new AnimatedButton(tr("Train on dataset")); m_btnTrain->setObjectName("labTrain");
    m_btnPause = new AnimatedButton(tr("Pause")); m_btnPause->setObjectName("labPause");
    m_btnPause->setToolTip(tr("Stops at the next step and keeps a resumable checkpoint"));
    m_btnResume = new AnimatedButton(tr("Resume")); m_btnResume->setObjectName("labResume");
    m_btnCancel = new AnimatedButton(tr("Cancel")); m_btnCancel->setObjectName("labCancel");
    for (auto *b : {m_btnTrain, m_btnPause, m_btnResume, m_btnCancel}) tb->addWidget(b);
    tf->addRow(tb);
    right->addWidget(tg);
    m_progress = new SmoothProgressBar; m_progress->setObjectName("labProgress");
    m_step = new QLabel; m_step->setObjectName("muted");
    right->addWidget(m_progress); right->addWidget(m_step);
    m_memory = new QLabel(tr("Memory: the estimate appears here.")); m_memory->setObjectName("labMemory"); m_memory->setWordWrap(true);
    right->addWidget(m_memory);
    auto *eg = new QGroupBox(tr("Decide, evaluate, grow"));
    auto *el = new QGridLayout(eg);
    m_btnAccept = new AnimatedButton(tr("Replace current model with candidate")); m_btnAccept->setObjectName("labAccept");
    m_btnReject = new AnimatedButton(tr("Discard candidate")); m_btnReject->setObjectName("labReject");
    m_btnEval = new AnimatedButton(tr("Evaluate current model")); m_btnEval->setObjectName("labEval");
    m_target = new QSpinBox; m_target->setRange(1000, 2000000000); m_target->setSingleStep(100000); m_target->setValue(1000000); m_target->setObjectName("labTarget");
    m_target->setToolTip(tr("Target parameter count. Expansion adds capacity, not knowledge: the larger model must be trained."));
    m_btnExpand = new AnimatedButton(tr("Expand current model")); m_btnExpand->setObjectName("labExpand");
    el->addWidget(m_btnAccept, 0, 0); el->addWidget(m_btnReject, 0, 1);
    el->addWidget(m_btnEval, 1, 0, 1, 2);
    el->addWidget(m_target, 2, 0); el->addWidget(m_btnExpand, 2, 1);
    right->addWidget(eg);
    right->addStretch(1);
    cols->addLayout(right, 4);
    root->addLayout(cols, 3);

    m_chart = new LossChart;
    root->addWidget(m_chart, 2);
    auto *bottom = new QHBoxLayout;
    m_eval = new QPlainTextEdit; m_eval->setObjectName("labEvalText"); m_eval->setReadOnly(true);
    m_eval->setPlaceholderText(tr("Results of training, evaluation and expansion appear here."));
    m_log = new QPlainTextEdit; m_log->setObjectName("labLog"); m_log->setReadOnly(true); m_log->setMaximumBlockCount(1500);
    m_log->setPlaceholderText(tr("Worker log and errors"));
    bottom->addWidget(m_eval, 3); bottom->addWidget(m_log, 2);
    root->addLayout(bottom, 2);

    connect(m_btnData, &QPushButton::clicked, this, [this] { createDataset(); });
    connect(m_btnCreate, &QPushButton::clicked, this, [this] { createModel(); });
    connect(m_btnTrain, &QPushButton::clicked, this, [this] { startTraining(false); });
    connect(m_btnLearn, &QPushButton::clicked, this, [this] { learnFromMySpeech(); });
    connect(m_btnResume, &QPushButton::clicked, this, [this] { startTraining(true); });
    connect(m_btnPause, &QPushButton::clicked, this, [this] { pauseTraining(); });
    connect(m_btnCancel, &QPushButton::clicked, this, [this] { cancelTraining(); });
    connect(m_btnEval, &QPushButton::clicked, this, [this] { evaluateModel(); });
    connect(m_btnExpand, &QPushButton::clicked, this, [this] { expandModel(); });
    connect(m_btnAccept, &QPushButton::clicked, this, [this] { acceptCandidate(); });
    connect(m_btnReject, &QPushButton::clicked, this, [this] { rejectCandidate(); });
    connect(m_btnForget, &QPushButton::clicked, this, [this] { deleteMyData(); });
    connect(m_btnExport, &QPushButton::clicked, this, [this] {
        const QString dir = QFileDialog::getExistingDirectory(this, tr("Export my stored speech data to..."));
        if (dir.isEmpty()) return;
        m_client->call("experience", {{"op", "export"}, {"dest", dir}}, this, [this](const QString &st, const QJsonObject &r) {
            if (st != "success") { showError(r.value("code").toString(), r.value("message").toString()); return; }
            m_status->setText(tr("Exported %1 records to %2").arg(r.value("records").toInt()).arg(r.value("path").toString()));
        });
    });
    connect(m_consent, &QCheckBox::toggled, this, [this](bool on) { if (!m_updatingConsent) setLearning(on); });
    connect(m_batch, qOverload<int>(&QSpinBox::valueChanged), this, [this] { requestEstimate(); });
    updateButtons();
}

void ModelLabDialog::log(const QString &line) { m_log->appendPlainText(line); }

void ModelLabDialog::setPhase(Phase p, const QString &text) {
    m_phase = p;
    if (!text.isEmpty()) m_status->setText(text);
    StateIcon::Kind k = StateIcon::Kind::Busy;
    switch (p) {
    case Phase::Ready: k = StateIcon::Kind::Success; break;
    case Phase::Unavailable: k = StateIcon::Kind::Error; break;
    default: break;
    }
    m_icon->setKind(k);
    updateButtons();
    emit phaseChanged(int(p));
}

void ModelLabDialog::showError(const QString &code, const QString &message) {
    m_lastError = (code.isEmpty() ? QString() : code + ": ") + message;
    log("[error] " + m_lastError);
    m_status->setText(m_lastError);
}

QJsonObject ModelLabDialog::modelById(const QString &id) const {
    for (const auto &v : m_models) if (v.toObject().value("id").toString() == id) return v.toObject();
    return {};
}

QString ModelLabDialog::selectedDataset() const { return m_dataset->currentData().toString(); }
int ModelLabDialog::modelCount() const { return m_tree->topLevelItemCount(); }

void ModelLabDialog::setHyperparameters(int steps, int batch, double lr, int sizeIndex) {
    m_steps->setValue(steps); m_batch->setValue(batch); m_lr->setValue(lr);
    if (sizeIndex >= 0) m_size->setCurrentIndex(sizeIndex);
}

void ModelLabDialog::updateButtons() {
    const bool ready = m_phase == Phase::Ready;
    const bool training = m_phase == Phase::Training;
    const bool haveModel = !m_activeId.isEmpty();
    const QJsonObject cand = modelById(m_candidateId);
    m_btnData->setEnabled(ready);
    m_btnCreate->setEnabled(ready);
    m_btnCreate->setText(haveModel ? tr("Replace with a new random model") : tr("Create random model"));
    m_btnTrain->setEnabled(ready && haveModel && m_dataset->count() > 0);
    m_btnLearn->setEnabled(ready && haveModel);
    m_btnPause->setEnabled(training);
    m_btnCancel->setEnabled(training);
    m_btnResume->setEnabled(ready && haveModel && !m_resumeRun.isEmpty());
    m_btnEval->setEnabled(ready && haveModel && m_dataset->count() > 0);
    m_btnExpand->setEnabled(ready && haveModel);
    m_btnAccept->setEnabled(ready && cand.value("state").toString() == "eval_passed");
    m_btnReject->setEnabled(ready && !m_candidateId.isEmpty());
    m_consent->setEnabled(m_phase != Phase::Starting);
    m_btnForget->setEnabled(m_phase != Phase::Starting && m_experience.value("total").toInt() > 0);
    m_btnExport->setEnabled(m_phase != Phase::Starting && m_experience.value("total").toInt() > 0);
}

void ModelLabDialog::refresh() {
    m_client->call("lab_status", {}, this, [this](const QString &st, const QJsonObject &r) {
        if (st != "success") { showError(r.value("code").toString(), r.value("message").toString()); return; }
        onModels(r);
    }, 60000);
}

void ModelLabDialog::updateExperience(const QJsonObject &e) {
    m_experience = e;
    m_updatingConsent = true;
    m_consent->setChecked(e.value("consent").toBool());
    m_updatingConsent = false;
    const int total = e.value("total").toInt();
    m_experienceLabel->setText(
        total == 0 ? tr("My speech: nothing stored. %1").arg(e.value("consent").toBool() ? tr("Learning is ON: generate speech, then run Recheck Speech to label it.")
                                                                                         : tr("Learning is OFF."))
                   : tr("My speech: %1 segments stored, %2 rechecked (%3 with issues, %4 verified), %5 waiting for Recheck Speech. Learning is %6. Stored in %7")
                         .arg(total).arg(e.value("labelled").toInt()).arg(e.value("yes").toInt()).arg(e.value("no").toInt())
                         .arg(e.value("unlabelled").toInt()).arg(e.value("consent").toBool() ? tr("ON") : tr("OFF"), e.value("path").toString()));
    updateButtons();
}

void ModelLabDialog::onModels(const QJsonObject &r) {
    m_models = r.value("models").toArray();
    m_datasets = r.value("datasets").toArray();
    m_activeId = r.value("active").toString();
    m_candidateId = r.value("candidate").toString();
    updateExperience(r.value("experience").toObject());
    m_tree->clear();
    for (const QString &id : {m_activeId, m_candidateId}) {
        const QJsonObject m = modelById(id);
        if (m.isEmpty()) continue;
        const QJsonObject res = m.value("metrics").toObject().value("evaluation").toObject().value("results").toObject();
        const double acc = res.value("val").toObject().value("accuracy").toDouble(-1);
        auto *it = new QTreeWidgetItem(m_tree);
        it->setText(0, id == m_activeId ? tr("Current model: %1").arg(m.value("name").toString()) : tr("Trained candidate (waiting for your decision)"));
        it->setText(1, params(qint64(m.value("param_count").toDouble())));
        it->setText(2, QString("%1 / %2 / %3").arg(m.value("layers").toInt()).arg(m.value("hidden").toInt()).arg(m.value("heads").toInt()));
        it->setText(3, stateText(m.value("state").toString(), false));
        it->setText(4, QString::number(m.value("steps").toInt()));
        it->setText(5, acc >= 0 ? pct(acc) : QStringLiteral("-"));
        it->setData(0, Qt::UserRole, id);
        it->setToolTip(0, m.value("checkpoint").toString());
    }
    m_stateLabel->setText(m_candidateId.isEmpty() ? stateText(modelById(m_activeId).value("state").toString(), false)
                                                  : tr("Candidate waiting for your decision"));
    const QString keepDs = selectedDataset();
    m_dataset->clear();
    for (const auto &v : m_datasets) {
        const QJsonObject d = v.toObject();
        m_dataset->addItem(QString("%1 %2 (%3 train)").arg(d.value("name").toString(), d.value("version").toString())
                               .arg(d.value("counts").toObject().value("train").toInt()), d.value("path").toString());
    }
    const int i = m_dataset->findData(keepDs);
    m_dataset->setCurrentIndex(i >= 0 ? i : m_dataset->count() - 1);
    updateButtons();
    requestEstimate();
}

void ModelLabDialog::requestEstimate() {
    if (m_activeId.isEmpty() || m_phase != Phase::Ready) return;
    m_client->call("estimate", {{"model_id", m_activeId}, {"batch_size", m_batch->value()}}, this, [this](const QString &st, const QJsonObject &r) {
        if (st != "success") return;
        const QJsonObject d = r.value("decision").toObject();
        m_lastEstimate = tr("Estimated peak training memory: %1 (weights+gradients %2, optimizer %3, activations %4). %5")
                             .arg(mb(r.value("total").toDouble()), mb(r.value("weights").toDouble() + r.value("gradients").toDouble()),
                                  mb(r.value("optimizer").toDouble()), mb(r.value("activations").toDouble()), d.value("reason").toString());
        m_memory->setText(m_lastActual.isEmpty() ? m_lastEstimate : m_lastEstimate + QLatin1Char(0x0A) + m_lastActual);
    });
}

void ModelLabDialog::setLearning(bool on) {
    m_client->call("experience", {{"op", "set_consent"}, {"enabled", on}}, this, [this](const QString &st, const QJsonObject &r) {
        if (st != "success") { showError(r.value("code").toString(), r.value("message").toString()); return; }
        updateExperience(r);
        m_status->setText(r.value("consent").toBool() ? tr("Learning is ON. Speech you generate from now on is remembered locally; run Recheck Speech to label it.")
                                                      : tr("Learning is OFF. Nothing new is being stored."));
    });
}

void ModelLabDialog::deleteMyData() {
    if (!m_confirm(tr("Delete stored speech data"), tr("Delete everything the model has remembered from your speech? This cannot be undone."))) return;
    m_client->call("experience", {{"op", "delete_all"}, {"confirmed", true}}, this, [this](const QString &st, const QJsonObject &r) {
        if (st != "success") { showError(r.value("code").toString(), r.value("message").toString()); return; }
        updateExperience(r);
        m_status->setText(tr("Deleted %1 stored segments.").arg(r.value("deleted").toInt()));
    });
}

void ModelLabDialog::createDataset() {
    if (m_phase != Phase::Ready) return;
    setPhase(Phase::Busy, tr("Creating synthetic dataset..."));
    m_client->call("make_dataset", {{"n_per_label", 400}}, this, [this](const QString &st, const QJsonObject &r) {
        setPhase(Phase::Ready);
        if (st != "success") { showError(r.value("code").toString(), r.value("message").toString()); return; }
        const QJsonObject m = r.value("manifest").toObject();
        m_status->setText(tr("Dataset %1 created (synthetic; labels correct by construction)").arg(m.value("version").toString()));
        log(m.value("provenance").toString());
        refresh();
    }, 300000);
}

void ModelLabDialog::createModel() {
    if (m_phase != Phase::Ready) return;
    const bool replace = !m_activeId.isEmpty();
    if (replace && !m_confirm(tr("Replace the model"), tr("A model already exists. Creating a new random model deletes the current model and any trained "
                                                           "candidate. Continue?"))) return;
    setPhase(Phase::Busy, tr("Creating randomly initialized model..."));
    QJsonObject p{{"name", m_name->text().trimmed()}, {"size", m_size->currentText()}, {"replace", replace}};
    if (!selectedDataset().isEmpty()) p["dataset"] = selectedDataset();
    m_client->call("create_main", p, this, [this](const QString &st, const QJsonObject &r) {
        setPhase(Phase::Ready);
        if (st != "success") { showError(r.value("code").toString(), r.value("message").toString()); return; }
        m_status->setText(tr("Created a randomly initialized model with %1 parameters.").arg(qint64(r.value("model").toObject().value("param_count").toDouble())));
        m_resumeRun.clear();
        refresh();
    }, 300000);
}

void ModelLabDialog::startTraining(bool resume, bool confirmed) {
    if (m_phase != Phase::Ready || m_activeId.isEmpty() || selectedDataset().isEmpty()) return;
    QJsonObject p{{"dataset", selectedDataset()}, {"steps", m_steps->value()}, {"batch_size", m_batch->value()}, {"lr", m_lr->value()},
                  {"confirmed", confirmed}, {"eval_every", qMax(10, m_steps->value() / 10)}, {"ckpt_every", qMax(50, m_steps->value() / 5)}};
    if (resume) p["resume_run"] = m_resumeRun;
    runTrainingJob("train", p, resume);
}

void ModelLabDialog::learnFromMySpeech(bool confirmed) {
    if (m_phase != Phase::Ready || m_activeId.isEmpty()) return;
    QJsonObject p{{"steps", m_steps->value()}, {"batch_size", m_batch->value()}, {"lr", m_lr->value()}, {"confirmed", confirmed},
                  {"eval_every", qMax(10, m_steps->value() / 10)}, {"ckpt_every", qMax(50, m_steps->value() / 5)}};
    runTrainingJob("learn", p, false);
}

void ModelLabDialog::runTrainingJob(const QString &action, const QJsonObject &params, bool resume) {
    m_chart->clear();
    m_progress->resetNow();
    m_jobId = m_client->request(action, params);
    setPhase(Phase::Training, resume ? tr("Resuming training...") : action == "learn" ? tr("Training on my speech...") : tr("Training..."));
    m_stateLabel->setText(stateText({}, true));
    disconnect(m_client, &ProtocolClient::finished, this, nullptr);
    connect(m_client, &ProtocolClient::finished, this, [this, action, params](const QString &id, const QString &status, const QJsonObject &r) {
        if (id != m_jobId) return;
        m_jobId.clear();
        const QJsonObject res = r.value("result").toObject();
        if (status == "success") {
            m_resumeRun.clear();
            m_progress->setTarget(100);
            const double peak = res.value("peak_rss_bytes").toDouble(), est = res.value("estimated_peak_bytes").toDouble();
            m_lastActual = tr("Last run - estimated peak: %1   Actual peak (worker process): %2   Device: %3")
                               .arg(mb(est), mb(peak), res.value("device").toString());
            m_memory->setText(m_lastEstimate.isEmpty() ? m_lastActual : m_lastEstimate + QLatin1Char(0x0A) + m_lastActual);
            showCandidateResult(res);
            setPhase(Phase::Ready);
            refresh();
        } else if (status == "cancelled") {
            m_resumeRun = res.value("run_dir").toString();
            setPhase(Phase::Ready, tr("Training stopped at step %1. A resumable checkpoint was kept.").arg(res.value("steps_done").toInt()));
            m_stateLabel->setText(tr("Training paused (checkpoint kept)"));
        } else {
            const QString code = r.value("code").toString(), msg = r.value("message").toString();
            setPhase(Phase::Ready);
            if (code == "CONFIRMATION_NEEDED") {
                if (m_confirm(tr("Memory warning"), msg + "\n\n" + tr("Start anyway?"))) {
                    QJsonObject again = params;
                    again["confirmed"] = true;
                    runTrainingJob(action, again, false);
                }
                return;
            }
            showError(code, msg);
            m_stateLabel->setText(tr("Training did not run"));
        }
    });
}

void ModelLabDialog::showCandidateResult(const QJsonObject &res) {
    const QJsonObject cand = res.value("candidate").toObject();
    const QJsonObject rr = res.value("results").toObject();
    QString t = tr("Training %1: %2 steps in %3 s. Final train loss %4, best validation loss %5.\n\n")
                    .arg(res.value("status").toString()).arg(res.value("steps_done").toInt()).arg(res.value("elapsed_s").toDouble(), 0, 'f', 1)
                    .arg(res.value("final_train_loss").toDouble(), 0, 'f', 4).arg(res.value("best_val_loss").toDouble(), 0, 'f', 4);
    const QJsonObject ds = res.value("experience_dataset").toObject();
    if (!ds.isEmpty()) t += tr("Trained on %1: %2 examples (provenance: your own rechecked speech; labels come from Recheck Speech and are imperfect).\n\n")
                                .arg(ds.value("version").toString()).arg(ds.value("counts").toObject().value("train").toInt());
    if (!cand.isEmpty()) {
        const bool passed = cand.value("state").toString() == "eval_passed";
        t += passed ? tr("CANDIDATE PASSED the checks against the current model.\nClick \"Replace current model with candidate\" to use it (the old model's files are deleted).\n\n")
                    : tr("CANDIDATE DID NOT PASS. The current model is unchanged. Discard the candidate or train it again.\n\n");
        for (const char *s : {"val", "regression"}) {
            const QJsonObject a = rr.value("active").toObject().value(s).toObject(), c = rr.value("candidate").toObject().value(s).toObject();
            t += tr("%1: current %2   candidate %3   (majority class %4, rule baseline %5, n=%6)\n").arg(QString(s), -11)
                     .arg(pct(a.value("accuracy").toDouble()), pct(c.value("accuracy").toDouble()), pct(c.value("majority_rate").toDouble()),
                          pct(c.value("baseline_accuracy").toDouble())).arg(c.value("n").toInt());
        }
        t += tr("\nChecks:\n");
        for (const auto &c : res.value("verdict").toObject().value("checks").toArray()) {
            const QJsonObject o = c.toObject();
            t += QString("  [%1] %2 - %3\n").arg(o.value("ok").toBool() ? "pass" : "FAIL", o.value("name").toString(), o.value("detail").toString());
        }
    }
    t += tr("\nLoss/perplexity are task metrics, not a speech-quality measure.");
    m_eval->setPlainText(t);
    m_status->setText(cand.isEmpty() ? tr("Training %1.").arg(res.value("status").toString())
                                     : cand.value("state").toString() == "eval_passed" ? tr("Training finished: the candidate passed. Replace the current model if you want it.")
                                                                                       : tr("Training finished: the candidate did not pass; the current model was kept."));
}

void ModelLabDialog::pauseTraining() { if (!m_jobId.isEmpty()) { m_client->cancel(m_jobId); m_status->setText(tr("Pausing at the next step...")); } }
void ModelLabDialog::cancelTraining() { m_resumeRun.clear(); pauseTraining(); }

void ModelLabDialog::evaluateModel() {
    if (m_phase != Phase::Ready || m_activeId.isEmpty() || selectedDataset().isEmpty()) return;
    setPhase(Phase::Evaluating, tr("Evaluating on validation, test and held-out regression sets..."));
    m_client->call("evaluate", {{"model_id", m_activeId}, {"dataset", selectedDataset()}}, this,
                   [this](const QString &st, const QJsonObject &r) {
        setPhase(Phase::Ready);
        if (st != "success") { showError(r.value("code").toString(), r.value("message").toString()); return; }
        showEvaluation(r);
        refresh();
    }, 600000);
}

void ModelLabDialog::showEvaluation(const QJsonObject &r) {
    QString t = tr("Dataset %1 - result: %2\n\n").arg(r.value("dataset").toString(), r.value("state").toString() == "eval_passed" ? tr("EVALUATION PASSED") : tr("EVALUATION FAILED"));
    for (const char *s : {"train", "val", "test", "regression"}) {
        const QJsonObject m = r.value("results").toObject().value(s).toObject();
        t += QString("%1: accuracy %2  macro-F1 %3  loss %4  | majority class %5  rule baseline %6  (n=%7)\n")
                 .arg(QString(s), -10).arg(pct(m.value("accuracy").toDouble())).arg(m.value("macro_f1").toDouble(), 0, 'f', 3)
                 .arg(m.value("loss").toDouble(), 0, 'f', 4).arg(pct(m.value("majority_rate").toDouble()))
                 .arg(pct(m.value("baseline_accuracy").toDouble())).arg(m.value("n").toInt());
    }
    t += tr("\nAcceptance checks:\n");
    for (const auto &c : r.value("verdict").toObject().value("checks").toArray()) {
        const QJsonObject o = c.toObject();
        t += QString("  [%1] %2 - %3\n").arg(o.value("ok").toBool() ? "pass" : "FAIL", o.value("name").toString(), o.value("detail").toString());
    }
    t += tr("\nLoss/perplexity are task metrics over the label tokens, not a speech-quality measure.");
    m_eval->setPlainText(t);
    m_stateLabel->setText(stateText(r.value("state").toString(), false));
}

void ModelLabDialog::expandModel() {
    if (m_phase != Phase::Ready || m_activeId.isEmpty()) return;
    if (!m_confirm(tr("Expand the model"), tr("Expansion replaces the current model with a larger one that starts from its weights; the smaller "
                                              "model's files are deleted. The larger model must be trained before it is any better. Continue?"))) return;
    setPhase(Phase::Busy, tr("Expanding..."));
    m_client->call("expand_active", {{"target_parameters", m_target->value()}}, this, [this](const QString &st, const QJsonObject &r) {
        setPhase(Phase::Ready);
        if (st != "success") { showError(r.value("code").toString(), r.value("message").toString()); return; }
        const QJsonObject rep = r.value("report").toObject();
        const QJsonObject meas = rep.value("measured").toObject();
        QString g;
        const QJsonObject growth = rep.value("growth").toObject();
        for (auto it = growth.begin(); it != growth.end(); ++it) g += QString("%1 %2->%3  ").arg(it.key()).arg(it.value().toArray().at(0).toInt()).arg(it.value().toArray().at(1).toInt());
        m_eval->setPlainText(tr("Expanded. The current model is now the larger one.\nParameters %1 -> %2\nGrowth: %3\n"
                                "Function-preserving by design: %4 (%5)\nMeasured on random inputs: max |logit diff| = %6, top-1 agreement = %7\n"
                                "Optimizer: %8\n\nExpansion adds capacity, not knowledge. Train it before relying on it.")
                                 .arg(qint64(rep.value("old_params").toDouble())).arg(qint64(rep.value("new_params").toDouble())).arg(g)
                                 .arg(rep.value("expected_function_preserving").toBool() ? tr("yes") : tr("no"), rep.value("preservation_note").toString())
                                 .arg(meas.value("max_abs_logit_diff").toDouble(), 0, 'g', 3).arg(meas.value("top1_agreement").toDouble(), 0, 'f', 3)
                                 .arg(rep.value("optimizer").toString()));
        m_status->setText(tr("Expanded; it needs training before it is any better."));
        refresh();
    }, 600000);
}

void ModelLabDialog::acceptCandidate() {
    const QJsonObject cand = modelById(m_candidateId);
    if (m_phase != Phase::Ready || cand.value("state").toString() != "eval_passed") return;
    if (!m_confirm(tr("Replace the model"), tr("Replace the current model with the trained candidate? The current model's files are deleted."))) return;
    m_client->call("accept_candidate", {}, this, [this](const QString &st, const QJsonObject &r) {
        if (st != "success") { showError(r.value("code").toString(), r.value("message").toString()); return; }
        m_status->setText(tr("The candidate is now your model; the previous model was deleted."));
        refresh();
    });
}

void ModelLabDialog::rejectCandidate() {
    if (m_phase != Phase::Ready || m_candidateId.isEmpty()) return;
    m_client->call("reject_candidate", {}, this, [this](const QString &st, const QJsonObject &r) {
        if (st != "success") { showError(r.value("code").toString(), r.value("message").toString()); return; }
        m_status->setText(tr("Candidate discarded; your model is unchanged."));
        refresh();
    });
}
