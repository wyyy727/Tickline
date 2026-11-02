"""LHM 冒烟测试：加载 dll、开 Computer、把温度传感器打出来。"""

import sys

LHM_DIR = r"D:\workspace\Tickline-space\tools\lhm"
sys.path.append(LHM_DIR)

import clr

clr.AddReference(LHM_DIR + r"\LibreHardwareMonitorLib.dll")

from LibreHardwareMonitor.Hardware import Computer, SensorType


def main() -> int:
    computer = Computer()
    computer.IsCpuEnabled = True
    computer.IsGpuEnabled = True
    computer.IsMemoryEnabled = True
    computer.IsMotherboardEnabled = True
    computer.Open()
    try:
        for hardware in computer.Hardware:
            hardware.Update()
            print(f"[{hardware.HardwareType}] {hardware.Name}")
            for sub in hardware.SubHardware:
                sub.Update()
                print(f"   └ [{sub.HardwareType}] {sub.Name}")
                for sensor in sub.Sensors:
                    if sensor.SensorType == SensorType.Temperature and sensor.Value is not None:
                        print(f"       {sensor.Name} = {sensor.Value:.1f} °C")
            for sensor in hardware.Sensors:
                if sensor.SensorType == SensorType.Temperature and sensor.Value is not None:
                    print(f"   {sensor.Name} = {sensor.Value:.1f} °C")
    finally:
        computer.Close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
