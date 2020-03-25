// Copyright 2014-2020 Josh Pieper, jjp@pobox.com.  All rights reserved.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#pragma once

#include <sched.h>

#include <boost/date_time/posix_time/posix_time.hpp>
#include <boost/filesystem.hpp>

#include <fmt/format.h>

#include <clipp/clipp.h>

#include "mjlib/base/clipp.h"
#include "mjlib/base/clipp_archive.h"
#include "mjlib/base/fail.h"

#include "context_full.h"
#include "handler_util.h"
#include "logging.h"

namespace mjmech {
namespace base {

template <typename Module>
int safe_main(int argc, char**argv) {
  Context context;
  Module module(context);

  std::string config_file;
  std::string log_file;
  bool debug = false;
  bool log_short_name = false;
  double event_timeout_s = 0;
  double idle_timeout_s = 0;
  int cpu_affinity = -1;

  auto group = clipp::group(
      (clipp::option("c", "config") & clipp::value("", config_file)) %
      "read options from file",
      (clipp::option("l", "log") & clipp::value("", log_file)) %
      "write to log file",
      (clipp::option("L", "log_short_name").set(log_short_name)) %
      "do not insert timestamp in log file name",
      (clipp::option("d", "debug").set(debug)) %
      "disable real-time signals and other debugging hindrances",
      (clipp::option("rt.event_timeout_s") & clipp::value("", event_timeout_s)),
      (clipp::option("rt.idle_timeout_s") & clipp::value("", idle_timeout_s)),
      (clipp::option("rt.cpu_affinity") & clipp::value("", cpu_affinity))
  );

  group.push_back(MakeLoggingOptions());

  group.push_back(mjlib::base::ClippArchive("remote_debug.")
                  .Accept(context.remote_debug->parameters()).release());

  group.push_back(module.program_options());

  mjlib::base::ClippParse(argc, argv, group);

  InitLogging();

  if (!config_file.empty()) {
    std::ifstream inf(config_file);
    mjlib::base::system_error::throw_if(
        !inf.is_open(), "opening " + config_file);
    mjlib::base::ClippParseIni(inf, group);
  }

  if (!log_file.empty()) {
    // Make sure that the log file has a date and timestamp somewhere
    // in the name.
    namespace fs = boost::filesystem;
    fs::path log_file_path(log_file);
    std::string extension = log_file_path.extension().native();

    const auto now = boost::posix_time::microsec_clock::universal_time();
    std::string datestamp =
        fmt::format("{}-{:02d}{:02d}{:02d}",
                    to_iso_string(now.date()),
                    now.time_of_day().hours(),
                    now.time_of_day().minutes(),
                    now.time_of_day().seconds());

    const std::string stem = log_file_path.stem().native();

    fs::path stamped_path = log_file_path.parent_path() /
        fmt::format("{}-{}{}", stem, datestamp, extension);

    if (log_short_name) {
      context.telemetry_log->Open(log_file);
    } else {
      context.telemetry_log->Open(stamped_path.native());
    }
  }

  // TODO theamk: move this to logging.cc
  TextLogMessageSignal log_signal_mt;
  context.telemetry_registry->Register("text_log", &log_signal_mt);

  // TODO theamk: should this marshalling be done by telemetry log
  // itself?
  TextLogMessageSignal* log_signal = GetLogMessageSignal();
  log_signal->connect(
      [&log_signal_mt, &context](const TextLogMessage* msg) {
        const TextLogMessage msg_copy = *msg;
        boost::asio::post(
            context.executor,
            [msg_copy, &log_signal_mt]() {log_signal_mt(&msg_copy);});
      });

  //WriteTextLogToTelemetryLog(&context.telemetry_registry);

  std::shared_ptr<ErrorHandlerJoiner> joiner =
      std::make_shared<ErrorHandlerJoiner>(
          [&](mjlib::base::error_code ec) {
            mjlib::base::FailIf(ec);

            if (cpu_affinity >= 0) {
              std::cout << "Setting CPU affinity for main thread to: "
                        << cpu_affinity << "\n";
              cpu_set_t cpuset = {};
              CPU_ZERO(&cpuset);
              CPU_SET(cpu_affinity, &cpuset);

              mjlib::base::system_error::throw_if(
                  ::sched_setaffinity(0, sizeof(cpu_set_t), &cpuset) < 0);
            }

            std::cout << "Started!\n";

            context.rt_executor.set_options(
                [&]() {
                  mjlib::io::RealtimeExecutor::Options options;
                  options.event_timeout_ns = event_timeout_s * 1000000000;
                  options.idle_timeout_ns = idle_timeout_s * 1000000000;
                  return options;
                }());
          });

  context.remote_debug->AsyncStart(joiner->Wrap("starting remote_debug"));
  module.AsyncStart(joiner->Wrap("starting main module"));


  context.context.run();
  return 0;
}


template <typename Module>
int main(int argc, char**argv) {
  try {
    return safe_main<Module>(argc, argv);
  } catch (std::exception& e) {
    std::cerr << "Error: " << e.what() << "\n";
    return 1;
  } catch (...) {
    std::cerr << "Unknown error.\n";
    return 2;
  }
}
}
}
