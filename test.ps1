Set-Location $PSScriptRoot
python -m pip install -q -r requirements.txt
python -m unittest tests.test_prod -v
