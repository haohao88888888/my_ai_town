"""Updated 2026-10-21: isolated XML -> CSV -> native JMeter reports, not acceptance.

Never starts a load test, edits a source XML, deletes files or changes the environment.
Report generation success is NOT acceptance of the rejected calibration workload.
"""
from __future__ import annotations

import csv
import hashlib
import json
import subprocess
import time
import zipfile
from pathlib import Path
from datetime import datetime, timezone
import xml.etree.ElementTree as ET


FIELDS = (
    ("timeStamp", "ts"), ("elapsed", "t"), ("label", "lb"),
    ("responseCode", "rc"), ("responseMessage", "rm"), ("threadName", "tn"),
    ("dataType", "dt"), ("success", "s"), ("failureMessage", None),
    ("bytes", "by"), ("sentBytes", "sby"), ("grpThreads", "ng"),
    ("allThreads", "na"), ("URL", None), ("Latency", "lt"),
    ("IdleTime", "it"), ("Connect", "ct"),
)

JAVA_SOURCE = '''// Updated 2026-10-21: artifact-only official dashboard API launcher.
import java.io.FileInputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import java.util.Properties;
import java.util.Set;
import org.apache.jmeter.util.JMeterUtils;
import org.apache.jmeter.report.dashboard.ReportGenerator;
import org.apache.jmeter.report.config.ReportGeneratorConfiguration;

class JMeterDashboardMain {
    public static void main(String[] args) throws Exception {
        if (args.length != 3) throw new IllegalArgumentException("home properties CSV required");
        JMeterUtils.setJMeterHome(args[0]);
        JMeterUtils.loadJMeterProperties(args[1]);
        JMeterUtils.initLocale();
        Properties props = new Properties();
        try (var in = new FileInputStream(Path.of(args[0], "bin", "reportgenerator.properties").toFile())) {
            props.load(in);
        }
        props.putAll(JMeterUtils.getJMeterProperties());
        var config = ReportGeneratorConfiguration.loadFromProperties(props);
        Set<String> allowed = Set.of("responseTimePercentiles", "responseTimeDistribution",
            "activeThreadsOverTime", "timeVsThreads", "bytesThroughputOverTime",
            "responseTimesOverTime", "responseTimePercentilesOverTime",
            "syntheticResponseTimeDistribution", "latenciesOverTime", "connectTimeOverTime",
            "hitsPerSecond", "codesPerSecond", "totalTPS", "transactionsPerSecond");
        if (!config.getGraphConfigurations().keySet().equals(allowed)) {
            throw new IllegalStateException("graph allowlist mismatch");
        }
        for (var entry : config.getGraphConfigurations().entrySet()) {
            Class<?> type = Class.forName(entry.getValue().getClassName());
            System.out.println("REGISTERED_MEMORY_GRAPH " + entry.getKey() + " " + type.getName());
            while (type != Object.class) {
                try (var in = type.getResourceAsStream(type.getSimpleName() + ".class")) {
                    String bytes = new String(in.readAllBytes(), StandardCharsets.ISO_8859_1);
                    if (bytes.contains("delete") || bytes.contains("ExternalSampleSorter")
                            || bytes.contains("SampleWriterConsumer")
                            || type.getName().contains("AbstractVersusRequestsGraphConsumer")) {
                        throw new IllegalStateException("unsafe consumer: " + type.getName());
                    }
                }
                type = type.getSuperclass();
            }
        }
        if (!Files.isDirectory(config.getTempDirectory().toPath())) {
            throw new IllegalStateException("preexisting report scratch required");
        }
        new ReportGenerator(args[2], null).generate();
    }
}
'''

MEMORY_GRAPHS = {
    "responseTimePercentiles", "responseTimeDistribution", "activeThreadsOverTime",
    "timeVsThreads", "bytesThroughputOverTime", "responseTimesOverTime",
    "responseTimePercentilesOverTime", "syntheticResponseTimeDistribution",
    "latenciesOverTime", "connectTimeOverTime", "hitsPerSecond", "codesPerSecond",
    "totalTPS", "transactionsPerSecond",
}


def isolated_report_config(  # pylint: disable=too-many-locals
    output: Path, installation: Path,
) -> tuple[Path, Path]:
    """Config snapshots only; excludes the two native deleting disk-buffer graphs."""
    home = output / "report-config-home"
    config_bin = home / "bin"
    config_bin.mkdir(parents=True, exist_ok=False)
    original = installation / "bin" / "reportgenerator.properties"
    lines, dropped, graph_classes = [], [], {}
    for line in original.read_text(encoding="ascii").splitlines(keepends=True):
        if line.startswith("jmeter.reportgenerator.graph."):
            graph = line.split(".")[3]
            if graph not in MEMORY_GRAPHS:
                if graph not in {"responseTimeVsRequest", "latencyVsRequest"}:
                    raise ValueError(f"unreviewed graph {graph}")
                dropped.append(line.rstrip())
                continue
            if ".classname=" in line:
                graph_classes[graph] = line.split("=", 1)[1].strip()
        lines.append(line)
    if set(graph_classes) != MEMORY_GRAPHS:
        raise ValueError("native graph allowlist mismatch")
    with (config_bin / "reportgenerator.properties").open("x", encoding="ascii") as stream:
        stream.writelines(lines)
    save_source = installation / "bin" / "saveservice.properties"
    with (config_bin / "saveservice.properties").open("xb") as stream:
        stream.write(save_source.read_bytes())
    log_config = output / "file-only-log4j2.xml"
    log_xml = '''<?xml version="1.0" encoding="UTF-8"?>
<Configuration status="WARN"><Appenders>
<File name="report-file" fileName="${sys:jmeter.logfile}" append="false">
<PatternLayout pattern="%d %p %c{1.}: %m%n"/>
</File></Appenders><Loggers><Root level="info">
<AppenderRef ref="report-file"/></Root></Loggers></Configuration>
'''
    with log_config.open("x", encoding="utf-8") as stream:
        stream.write(log_xml)
    jar = installation / "lib" / "ext" / "ApacheJMeter_core.jar"
    with zipfile.ZipFile(jar) as archive:
        deletion_classes = [name for name in archive.namelist()
                            if name.startswith("org/apache/jmeter/report/")
                            and name.endswith(".class")
                            and any(word in archive.read(name) for word in
                                    (b"delete", b"cleanDirectory", b"forceDelete"))]
    expected_deleters = {"org/apache/jmeter/report/" + suffix + ".class" for suffix in (
        "dashboard/ReportGenerator", "processor/ExternalSampleSorter",
        "processor/SampleWriterConsumer",
        "processor/graph/AbstractVersusRequestsGraphConsumer$TimeCountConsumer")}
    if set(deletion_classes) != expected_deleters:
        raise ValueError("Report library deletion paths changed; source review required")
    fresh_json(output / "consumer-safety-audit.json", {
        "updatedAtUtc": datetime.now(timezone.utc).isoformat(),
        "nativeCoreJarSha256": sha256(jar), "registeredMemoryGraphs": graph_classes,
        "removedNonmandatoryGraphDefinitions": dropped,
        "requiredStatisticsAndAllSamplesRetained": True,
        "classesContainingDeletionSymbols": deletion_classes,
        "topLevelDeleteGuard": "preexisting scratch => tmpDirCreated false",
        "excludedConsumer": "AbstractVersusRequestsGraphConsumer and its TimeCountConsumer",
        "unregisteredConsumers": ["ExternalSampleSorter", "SampleWriterConsumer"],
        "prefsObservationLimit": "no warnings is not an OS audit of all registry access",
        "source": "https://github.com/apache/jmeter/blob/rel/v5.6.3/src/core/src/main/java/"
                  "org/apache/jmeter/report/processor/graph/"
                  "AbstractVersusRequestsGraphConsumer.java",
    })
    return home, log_config


def sha256(path: Path) -> str:
    """Digest original artifact bytes."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fresh_json(path: Path, value) -> None:
    """Never overwrite prior evidence."""
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def window_csv(  # pylint: disable=too-many-locals
    xml_path: Path, output: Path, start: int, end: int,
) -> dict:
    """Exact half-open start-timestamp window; all failures retained, no sorting.

    Standard CSV contains all original JMeter scalars, preserving CSV quoting.
    Sidecar maps each CSV data row (1-based) to original JTL sample index (0-based).
    Additional assertion messages stay lossless in the sidecar and original XML.
    """
    if start >= end:
        raise ValueError("empty/reversed window")
    original_sha = sha256(xml_path)
    tree = ET.parse(xml_path).getroot()
    if tree.tag != "testResults" or any(e.tag != "httpSample" for e in tree):
        raise ValueError("expected flat HTTP JTL, not nested/controller samples")
    selected, references = [], []
    for index, sample in enumerate(tree):
        if not start <= int(sample.attrib["ts"]) < end:
            continue
        if sample.attrib["s"] not in {"true", "false"}:
            raise ValueError("invalid original success field")
        assertions = [{child.tag: child.text or "" for child in assertion}
                      for assertion in sample.findall("assertionResult")]
        failures = [a.get("failureMessage", "") for a in assertions
                    if a.get("failure") == "true" or a.get("error") == "true"]
        row = [sample.attrib[attr] if attr else
               (next((msg for msg in failures if msg), "") if name == "failureMessage"
                else sample.findtext("java.net.URL", "")) for name, attr in FIELDS]
        selected.append(row)
        references.append({
            "csvDataRow1Based": len(selected), "jtlSampleIndex0Based": index,
            "timestampEpochMs": int(sample.attrib["ts"]),
            "success": sample.attrib["s"] == "true", "assertions": assertions,
            "csvValuesSha256": hashlib.sha256(json.dumps(
                row, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
        })
    if not selected:
        raise ValueError("no measurement samples")
    csv_path = output / "measurement.csv"
    with csv_path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow([name for name, _ in FIELDS])
        writer.writerows(selected)
    with csv_path.open(encoding="utf-8", newline="") as stream:
        reader = csv.reader(stream)
        if next(reader) != [name for name, _ in FIELDS] or list(reader) != selected:
            raise AssertionError("CSV roundtrip changed original values")
    with (output / "measurement-source-map.jsonl").open("x", encoding="utf-8") as stream:
        for ref in references:
            stream.write(json.dumps(ref, ensure_ascii=False) + "\n")
    if sha256(xml_path) != original_sha:
        raise AssertionError("original XML changed")
    result = {
        "scope": "derived report, NOT a load test or acceptance pass",
        "originalXml": str(xml_path), "originalXmlSha256": original_sha,
        "csv": str(csv_path), "csvSha256": sha256(csv_path),
        "mappingSha256": sha256(output / "measurement-source-map.jsonl"),
        "windowStartEpochMsInclusive": start, "windowEndEpochMsExclusive": end,
        "originalSamples": len(tree), "measurementSamples": len(selected),
        "measurementFailures": sum(row[7] == "false" for row in selected),
        "selection": "start <= original ts < end, retain source order and failures",
        "failureMessageConvention": "first nonempty failing assertion, all assertions in mapping",
    }
    fresh_json(output / "conversion.json", result)
    return result


def native_html(  # pylint: disable=too-many-locals,too-many-statements
    output: Path, csv_path: Path, java: Path, jar: Path, timeout=90,
) -> dict:
    """Isolated, bounded ReportGenerator API only, no CLI GUI/prefs init.

    5.6.3 ReportGenerator.createTempDir returns false for an existing scratch dir;
    removeTempDir therefore does not recursively delete it. Fresh HTML prevents
    report overwrite, local properties and explicit exporter/template paths avoid global
    configuration writes or accidental inherited filters.
    """
    scratch, java_tmp, java_home = (output / name for name in
                                  ("report-scratch", "java-tmp", "java-home"))
    for directory in (scratch, java_tmp, java_home):
        directory.mkdir(exist_ok=False)
    sentinel = scratch / "retention-sentinel.txt"
    with sentinel.open("x", encoding="ascii") as stream:
        stream.write("Preexisting scratch: do not delete.\n")
    sentinel_sha = sha256(sentinel)
    html = output / "html"
    if html.exists():
        raise FileExistsError(html)
    template = jar.parent / "report-template"
    installation = jar.parent.parent
    config_home, log_config = isolated_report_config(output, installation)
    local_props = output / "local.properties"
    empty_props = output / "empty.properties"
    with empty_props.open("x", encoding="ascii"):
        pass
    with local_props.open("x", encoding="ascii") as stream:
        # Forward slashes avoid Java Properties backslash-unescaping Windows paths.
        stream.write(f"user.properties={empty_props.as_posix()}\n")
        stream.write(f"system.properties={empty_props.as_posix()}\n")
        stream.write("jmeter.save.saveservice.output_format=csv\n")
        stream.write("jmeter.save.saveservice.timestamp_format=ms\n")
        stream.write("jmeter.save.saveservice.default_delimiter=,\n")
        stream.write("jmeter.reportgenerator.sample_filter=\n")
        stream.write("jmeter.reportgenerator.exporter.html.series_filter=\n")
        # Native dashboard defaults only, NOT project p95/p99 acceptance rules.
        stream.write("jmeter.reportgenerator.apdex_satisfied_threshold=500\n")
        stream.write("jmeter.reportgenerator.apdex_tolerated_threshold=1500\n")
        stream.write(f"jmeter.reportgenerator.temp_dir={scratch.as_posix()}\n")
        stream.write(f"jmeter.reportgenerator.outputdir={html.as_posix()}\n")
        stream.write("jmeter.reportgenerator.exporter.html.property.template_dir="
                     + template.as_posix() + "\n")
        stream.write("jmeter.reportgenerator.report_title=Readonly measurements - NOT acceptance\n")
        stream.write("jmeter.reportgenerator.overall_granularity=1000\n")
    launcher = output / "JMeterDashboardMain.java"
    with launcher.open("x", encoding="utf-8") as stream:
        stream.write(JAVA_SOURCE)
    classpath = ";".join(str(item) for item in
                         (installation / "lib" / "*", installation / "lib" / "ext" / "*", jar))
    command = [str(java), "-Djava.awt.headless=true", "-XX:-UsePerfData",
               f"-Djava.io.tmpdir={java_tmp}", f"-Duser.home={java_home}",
               f"-Dlog4j.configurationFile={log_config.as_uri()}",
               f"-Djmeter.logfile={(output / 'report-jmeter.log').as_posix()}",
               "--class-path", classpath, str(launcher), str(config_home),
               str(local_props), str(csv_path)]
    started = time.monotonic()
    fresh_json(output / "command.json", {
        "command": command, "cwd": str(output), "timeoutSeconds": timeout,
        "startedAtUtc": datetime.now(timezone.utc).isoformat(), "environmentMutation": False,
        "preexistingScratchPreventsRecursiveCleanup": True,
        "javaSha256": sha256(java), "jarSha256": sha256(jar),
        "launcherSha256": sha256(launcher), "localPropertiesSha256": sha256(local_props),
        "mode": "native ReportGenerator API, Java source-file mode, NOT CLI or new test",
    })
    with (output / "report-console.log").open("xb") as console:
        with subprocess.Popen(command, cwd=output, stdout=console, stderr=subprocess.STDOUT,
                              creationflags=subprocess.CREATE_NO_WINDOW) as process:
            try:
                code = process.wait(timeout=timeout)
                timed_out = False
            except subprocess.TimeoutExpired:
                process.kill()
                code = process.wait(timeout=10)
                timed_out = True
            except BaseException:
                process.kill()
                process.wait(timeout=10)
                raise
    result = {"pid": process.pid, "exitCode": code, "timedOut": timed_out,
              "elapsedSeconds": time.monotonic() - started,
              "scratchExistsAfter": scratch.is_dir(),
              "scratchSentinelUnchanged": sentinel.is_file() and sha256(sentinel) == sentinel_sha,
              "indexExists": (html / "index.html").is_file()}
    console_text = (output / "report-console.log").read_text(encoding="utf-8", errors="replace")
    result["preferencesWarningsObserved"] = any(
        marker in console_text for marker in (
            "WindowsPreferences", "java.util.prefs", "RegCreateKey"))
    result["jmeterLogExists"] = (output / "report-jmeter.log").is_file()
    result["consoleConfigurationErrorObserved"] = "ERROR Status" in console_text
    report_log = (output / "report-jmeter.log").read_text(encoding="utf-8", errors="replace") \
        if result["jmeterLogExists"] else ""
    result["nativeErrorObserved"] = " ERROR " in report_log
    fresh_json(output / "report-process.json", result)
    valid = (not timed_out, code == 0, result["indexExists"],
             not result["preferencesWarningsObserved"], result["scratchSentinelUnchanged"],
             result["jmeterLogExists"], not result["consoleConfigurationErrorObserved"],
             not result["nativeErrorObserved"])
    if not all(valid):
        raise RuntimeError("native HTML generation failed; logs retained")
    return result


def build_report(root: Path, report: dict, java: Path, jar: Path) -> dict:
    """Create verified derived artifacts; the load decision remains independent."""
    output = root / "dashboard"
    output.mkdir(exist_ok=False)
    with (output / "report-source.py").open("xb") as stream:
        stream.write(Path(__file__).read_bytes())
    conversion = window_csv(root / "samples.jtl", output,
                            report["windowStartEpochMs"], report["windowEndEpochMs"])
    process = native_html(output, output / "measurement.csv", java, jar)
    stats = json.loads((output / "html/statistics.json").read_text(encoding="utf-8"))["Total"]
    if (stats["sampleCount"] != conversion["measurementSamples"]
            or stats["errorCount"] != conversion["measurementFailures"]
            or sha256(root / "samples.jtl") != report["jtlSha256"]):
        raise ValueError("Native report counts or original JTL identity mismatch")
    notes = (f"> 更新时间：{datetime.now(timezone.utc).isoformat()}（UTC）\n\n"
             "# Readonly measurement report interpretation\n\n"
             "This native dashboard is NOT a load acceptance decision. Its throughput uses "
             "sample span, not the project's fixed window; its aggregate percentiles can include "
             "failed attempts and use a different estimator. Use measurement-summary.json and "
             "target-decision.json for fixed-window successful-response nearest-rank criteria.\n\n"
             "All selected samples and failures remain in CSV/XML. responseTimeVsRequest and "
             "latencyVsRequest are deliberately absent: their native disk-buffer consumers "
             "automatically delete caches. Fourteen reviewed memory graphs and statistics remain. "
             "Default APDEX 500/1500ms is display configuration, NOT the approved SLA.\n\n"
             "No Preferences warning was observed in this report run; that is not an OS audit "
             "proving zero registry access. The preexisting scratch/sentinel is retained.\n")
    with (output / "REPORT-NOTES.md").open("x", encoding="utf-8") as stream:
        stream.write(notes)
    result = {"scope": "verified derived report only; NOT acceptance of failed requests",
              "conversion": conversion, "nativeProcess": process, "nativeStatistics": stats,
              "independentVerifiedFailureCount": report["measuredFailedSamples"],
              "sourceSha256": sha256(output / "report-source.py"),
              "files": {p.relative_to(output).as_posix(): sha256(p)
                        for p in sorted(output.rglob("*")) if p.is_file()}}
    fresh_json(output / "verified-report.json", result)
    return {"index": str(output / "html/index.html"), "samples": stats["sampleCount"],
            "failures": stats["errorCount"],
            "manifestSha256": sha256(output / "verified-report.json")}
